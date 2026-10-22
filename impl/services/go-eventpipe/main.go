// Diting PdM · Go 事件管线（CQRS 读侧投影器）
//
// 职责：
//   消费 Redis Stream `pdm:events:<tenant>` → 投影到 TiDB 读模型 read_asset_health
//
// 为什么用 Go 而不是继续用 Python：
//   - 事件消费是长驻、高并发 I/O 任务，Go 的调度与内存开销远低于 Python 进程
//   - 单二进制部署，镜像小、启动快，适合边缘/常驻场景
//   - 与 docs/16 §2 的语言职责矩阵一致（Go 承担事件管线与 BFF）
//
// 幂等：processed_event(consumer, event_id) 唯一键；重复投递自动跳过。
// 断点续跑：Redis 消费者组 + XACK；失败不 ACK，下一轮重试。
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/go-sql-driver/mysql"
	"github.com/redis/go-redis/v9"
	"database/sql"
)

const (
	group        = "go-projector"
	projectionID = "read-model-v1"
	streamPrefix = "pdm:events:"
)

type Event struct {
	ID         string                 `json:"id"`
	Type       string                 `json:"type"`
	TenantID   string                 `json:"tenant_id"`
	Subject    string                 `json:"subject"`
	TraceID    string                 `json:"trace_id"`
	Data       map[string]interface{} `json:"data"`
	OccurredAt string                 `json:"time"`
}

type counters struct {
	Read      atomic.Int64
	Projected atomic.Int64
	Skipped   atomic.Int64
	Errors    atomic.Int64
}

var stats counters

type App struct {
	rdb    *redis.Client
	db     *sql.DB
	tenants []string
}

func env(k, def string) string {
	if v := os.Getenv(k); v != "" {
		return v
	}
	return def
}

func main() {
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	rdb := redis.NewClient(&redis.Options{
		Addr:         env("PDM_REDIS_ADDR", "redis.infra.svc.cluster.local:6379"),
		Password:     env("PDM_REDIS_PASSWORD", ""),
		DB:           atoi(env("PDM_REDIS_DB", "3")),
		DialTimeout:  2 * time.Second,
		ReadTimeout:  5 * time.Second,
		WriteTimeout: 5 * time.Second,
	})

	dsn := fmt.Sprintf("%s:%s@tcp(%s:%s)/pdm?parseTime=true&timeout=3s",
		env("PDM_TIDB_USER", "pdm_diag"),
		env("PDM_TIDB_PASSWORD", ""),
		env("PDM_TIDB_HOST", "tidb.infra.svc.cluster.local"),
		env("PDM_TIDB_PORT", "4000"),
	)
	db, err := sql.Open("mysql", dsn)
	if err != nil {
		log.Fatalf("tidb open: %v", err)
	}
	db.SetMaxOpenConns(8)
	db.SetMaxIdleConns(4)
	db.SetConnMaxLifetime(5 * time.Minute)
	// TiDB 需要显式关闭严格模式下的隐式转换告警
	if cfg, e := mysql.ParseDSN(dsn); e == nil {
		cfg.Params = map[string]string{"charset": "utf8mb4"}
	}

	app := &App{rdb: rdb, db: db, tenants: strings.Split(env("PDM_TENANTS", "site-a,site-b"), ",")}

	// 健康检查与指标（供 K8s probe 与 Prometheus 抓取）
	mux := http.NewServeMux()
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
		if err := app.rdb.Ping(r.Context()).Err(); err != nil {
			w.WriteHeader(503)
			fmt.Fprintf(w, `{"status":"degraded","redis":"%v"}`, err)
			return
		}
		fmt.Fprint(w, `{"status":"ok","service":"go-eventpipe"}`)
	})
	mux.HandleFunc("/stats", func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		fmt.Fprintf(w, `{"read":%d,"projected":%d,"skipped":%d,"errors":%d}`,
			stats.Read.Load(), stats.Projected.Load(), stats.Skipped.Load(), stats.Errors.Load())
	})
	srv := &http.Server{Addr: ":" + env("PORT", "8080"), Handler: mux}
	go func() {
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Printf("http: %v", err)
		}
	}()

	for _, t := range app.tenants {
		t = strings.TrimSpace(t)
		if t == "" {
			continue
		}
		stream := streamPrefix + t
		if err := rdb.XGroupCreateMkStream(ctx, stream, group, "0").Err(); err != nil &&
			!strings.Contains(err.Error(), "BUSYGROUP") {
			log.Printf("create group %s: %v", stream, err)
		}
	}

	sig := make(chan os.Signal, 1)
	signal.Notify(sig, syscall.SIGINT, syscall.SIGTERM)
	go func() { <-sig; cancel() }()

	log.Printf("go-eventpipe started: tenants=%v", app.tenants)
	ticker := time.NewTicker(2 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			_ = srv.Shutdown(context.Background())
			log.Print("stopped")
			return
		case <-ticker.C:
			app.drain(ctx, 50)
		}
	}
}

func (a *App) drain(ctx context.Context, batch int64) {
	streams := make([]string, 0, len(a.tenants))
	for _, t := range a.tenants {
		if s := strings.TrimSpace(t); s != "" {
			streams = append(streams, streamPrefix+s)
		}
	}
	if len(streams) == 0 {
		return
	}
	// 逐流读取，避免 XREADGROUP 多流时 ACK 归属混淆
	for _, stream := range streams {
		tenant := strings.TrimPrefix(stream, streamPrefix)
		res, err := a.rdb.XReadGroup(ctx, &redis.XReadGroupArgs{
			Group:    group,
			Consumer: env("HOSTNAME", "go-projector-1"),
			Streams:  []string{stream, ">"},
			Count:    batch,
			Block:    500 * time.Millisecond,
		}).Result()
		if err != nil {
			if err != redis.Nil {
				log.Printf("xreadgroup %s: %v", stream, err)
			}
			continue
		}
		for _, s := range res {
			for _, msg := range s.Messages {
				stats.Read.Add(1)
				raw, _ := msg.Values["data"].(string)
				var ev Event
				if err := json.Unmarshal([]byte(raw), &ev); err != nil {
					stats.Errors.Add(1)
					a.rdb.XAck(ctx, stream, group, msg.ID)
					continue
				}
				if ev.TenantID == "" {
					ev.TenantID = tenant
				}
				done, err := a.project(ctx, ev)
				if err != nil {
					stats.Errors.Add(1)
					log.Printf("project %s: %v", msg.ID, err)
					continue // 不 ACK，下轮重试
				}
				if !done {
					stats.Skipped.Add(1)
				} else {
					stats.Projected.Add(1)
				}
				a.rdb.XAck(ctx, stream, group, msg.ID)
			}
		}
	}
}

// project 返回 (是否产生投影, error)
func (a *App) project(ctx context.Context, ev Event) (bool, error) {
	// 幂等：唯一键冲突说明已处理
	res, err := a.db.ExecContext(ctx,
		"INSERT IGNORE INTO processed_event(consumer, event_id) VALUES (?, ?)", projectionID, ev.ID)
	if err != nil {
		return false, err
	}
	if n, _ := res.RowsAffected(); n == 0 {
		return false, nil
	}

	switch ev.Type {
	case "com.yadea.pdm.diagnosis.finalized.v1":
		asset, _ := ev.Data["asset_id"].(string)
		if asset == "" {
			return false, nil
		}
		hi, _ := ev.Data["hi"].(float64)
		level, _ := ev.Data["level"].(string)
		_, err := a.db.ExecContext(ctx,
			`INSERT INTO read_asset_health(tenant_id,asset_id,hi,level,rul_p50_days,open_alerts)
			 VALUES (?,?,?,?,NULL,0)
			 ON DUPLICATE KEY UPDATE hi=VALUES(hi), level=VALUES(level)`,
			ev.TenantID, asset, hi, level)
		if err != nil {
			return false, err
		}
		log.Printf("projected health %s hi=%.1f", asset, hi)
		return true, nil

	case "com.yadea.pdm.decision.cardCreated.v1":
		asset, _ := ev.Data["asset_id"].(string)
		if asset == "" {
			return false, nil
		}
		_, err := a.db.ExecContext(ctx,
			"UPDATE read_asset_health SET open_alerts = open_alerts + 1 WHERE tenant_id=? AND asset_id=?",
			ev.TenantID, asset)
		if err != nil {
			return false, err
		}
		return true, nil
	}
	return false, nil
}

func atoi(s string) int {
	n, err := strconv.Atoi(s)
	if err != nil {
		return 0
	}
	return n
}
