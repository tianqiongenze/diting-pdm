// Diting PdM · Rust 边缘采集器（零依赖，仅 std）
//
// 为什么零依赖：
//   - 工业边缘要长期稳定运行，依赖越少越可靠
//   - 避免 crates.io 拉取与长编译（现场部署时往往没有外网）
//   - HTTP/1.1 手写即可满足"上报特征值"这一单一职责
//
// 职责（对应 docs/03 §5 边缘层）：
//   1. 采集/模拟波形 → 本地做 DSP（RMS / 峭度 / 峰值因子）
//   2. 只上行特征值，不上行原始波形（数据不出厂）
//   3. 上报失败时进入降级：本地打印告警，缓存待补传
//
// 用法：
//   edge-collector --api http://10.167.2.175:32231/pdm-api --tenant site-a \
//                  --asset FAN-00004 --loops 5 --interval 1
//
// 编译：rustc -O main.rs -o edge-collector   （无需 cargo）

use std::env;
use std::io::{Read, Write};
use std::net::TcpStream;
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

#[derive(Clone)]
struct Config {
    host: String,
    port: u16,
    path_prefix: String,
    tenant: String,
    asset: String,
    loops: u32,
    interval_ms: u64,
    fs: f64,
    host_header: Option<String>,
}

struct Features {
    rms: f64,
    peak: f64,
    kurtosis: f64,
    crest: f64,
}

/// 确定性伪随机（xorshift），保证同一 seed 可复现，便于现场比对
fn next_rand(state: &mut u64) -> f64 {
    let mut x = *state;
    x ^= x << 13;
    x ^= x >> 7;
    x ^= x << 17;
    *state = x;
    (x as f64) / (u64::MAX as f64)
}

/// 生成一段模拟振动波形：基频 + 噪声 + 周期性冲击（模拟轴承点蚀）
fn simulate(fs: f64, seconds: f64, base: f64, impulse: bool, seed: u64) -> Vec<f64> {
    let n = (fs * seconds) as usize;
    let mut out = Vec::with_capacity(n);
    let mut st = seed | 1;
    let f0 = 25.0; // 转频 25Hz（1500rpm）
    for i in 0..n {
        let t = i as f64 / fs;
        let carrier = (2.0 * std::f64::consts::PI * 4000.0 * t).sin(); // 共振载波 4kHz
        let mut v = base * (2.0 * std::f64::consts::PI * f0 * t).sin();
        v += (next_rand(&mut st) - 0.5) * base * 0.4;
        if impulse && (i % (fs as usize / 87) == 0) {
            // 87Hz 冲击调制（对应 BPFO 示例）
            v += carrier * 3.5;
        }
        out.push(v);
    }
    out
}

fn features(x: &[f64]) -> Features {
    let n = x.len() as f64;
    let mean = x.iter().sum::<f64>() / n;
    let var = x.iter().map(|v| (v - mean).powi(2)).sum::<f64>() / n;
    let sd = var.sqrt();
    let rms = (x.iter().map(|v| v * v).sum::<f64>() / n).sqrt();
    let peak = x.iter().fold(0.0_f64, |a, v| a.max(v.abs()));
    let kurt = if sd > 1e-12 {
        x.iter().map(|v| ((v - mean) / sd).powi(4)).sum::<f64>() / n
    } else {
        3.0
    };
    Features { rms, peak, kurtosis: kurt, crest: if rms > 1e-12 { peak / rms } else { 0.0 } }
}

fn now_ms() -> u128 {
    SystemTime::now().duration_since(UNIX_EPOCH).unwrap().as_millis()
}

/// 手写 HTTP/1.1 POST（Connection: close，避免 keep-alive 状态管理）
fn http_post(host: &str, port: u16, path: &str, tenant: &str, body: &str, host_header: Option<&str>) -> Result<String, String> {
    let host_hdr = host_header.unwrap_or(host);
    let mut stream = TcpStream::connect((host, port)).map_err(|e| format!("connect: {e}"))?;
    stream
        .set_read_timeout(Some(Duration::from_secs(10)))
        .map_err(|e| format!("set_timeout: {e}"))?;
    let req = format!(
        "POST {path} HTTP/1.1\r\nHost: {host_hdr}\r\nContent-Type: application/json\r\n\
         X-Tenant-Id: {tenant}\r\nContent-Length: {len}\r\nConnection: close\r\n\r\n{body}",
        len = body.len()
    );
    stream.write_all(req.as_bytes()).map_err(|e| format!("write: {e}"))?;

    let mut raw = String::new();
    stream.read_to_string(&mut raw).map_err(|e| format!("read: {e}"))?;
    let status = raw.lines().next().unwrap_or("").to_string();
    if !status.contains(" 200") {
        return Err(format!("http status: {status}"));
    }
    let body_start = raw.find("\r\n\r\n").map(|i| i + 4).unwrap_or(0);
    Ok(raw[body_start..].to_string())
}

fn parse_args() -> Config {
    let args: Vec<String> = env::args().collect();
    let mut cfg = Config {
        host: "10.167.2.175".into(),
        port: 32231,
        path_prefix: "/pdm-api".into(),
        tenant: "site-a".into(),
        asset: "FAN-00004".into(),
        loops: 3,
        interval_ms: 1000,
        fs: 25600.0,
        host_header: None,
    };
    let mut i = 1;
    while i + 1 < args.len() {
        let v = args[i + 1].clone();
        match args[i].as_str() {
            "--api" => {
                // 形如 http://host:port/prefix
                let s = v.trim_start_matches("http://");
                let (hp, prefix) = match s.find('/') {
                    Some(p) => (&s[..p], s[p..].to_string()),
                    None => (s, String::new()),
                };
                let (h, p) = match hp.find(':') {
                    Some(c) => (&hp[..c], hp[c + 1..].parse().unwrap_or(32231)),
                    None => (hp, 80),
                };
                cfg.host = h.to_string();
                cfg.port = p;
                cfg.path_prefix = prefix;
            }
            "--tenant" => cfg.tenant = v,
            "--asset" => cfg.asset = v,
            "--loops" => cfg.loops = v.parse().unwrap_or(3),
            "--interval" => cfg.interval_ms = v.parse::<u64>().unwrap_or(1) * 1000,
            "--fs" => cfg.fs = v.parse().unwrap_or(25600.0),
            "--host-header" => cfg.host_header = Some(v),
            _ => {}
        }
        i += 2;
    }
    cfg
}

fn main() {
    let cfg = parse_args();
    println!(
        "[edge] target={}:{} {}{} tenant={} asset={} fs={}",
        cfg.host, cfg.port, cfg.path_prefix, "/v1/diagnose", cfg.tenant, cfg.asset, cfg.fs
    );

    let mut pending = 0u32;
    for k in 0..cfg.loops {
        let seed = (now_ms() as u64) ^ (k as u64 * 7919);
        // 模拟劣化：后段注入冲击
        let impulse = k >= cfg.loops / 2;
        let wave = simulate(cfg.fs, 1.0, 2.0 + k as f64 * 0.4, impulse, seed);
        let f = features(&wave);

        println!(
            "[edge] #{k} local_dsp rms={:.3} peak={:.3} kurt={:.2} crest={:.2} impulse={}",
            f.rms, f.peak, f.kurtosis, f.crest, impulse
        );

        // 只上行特征值（原始波形留在边缘）
        let body = format!(
            "{{\"tenant_id\":\"{}\",\"asset_id\":\"{}\",\"metrics\":{{\"vib_rms_mm_s\":{:.4},\
             \"vib_baseline_mm_s\":2.0,\"kurtosis\":{:.4}}}}}",
            cfg.tenant, cfg.asset, f.rms, f.kurtosis
        );
        let path = format!("{}/v1/diagnose", cfg.path_prefix);

        match http_post(&cfg.host, cfg.port, &path, &cfg.tenant, &body, cfg.host_header.as_deref()) {
            Ok(resp) => {
                let hi = resp.split("\"hi\":").nth(1).and_then(|s| s.split([',', '}']).next());
                let lvl = resp.split("\"level\":\"").nth(1).and_then(|s| s.split('"').next());
                println!(
                    "[edge] #{k} uplink ok hi={} level={} pending={}",
                    hi.unwrap_or("?"),
                    lvl.unwrap_or("?"),
                    pending
                );
            }
            Err(e) => {
                // 降级：本地告警 + 缓存待补传（此处仅计数，生产应落盘）
                pending += 1;
                println!("[edge] #{k} DEGRADED uplink failed: {e} (cached={pending})");
            }
        }
        thread::sleep(Duration::from_millis(cfg.interval_ms));
    }
    println!("[edge] done, pending_uplink={pending}");
}
