package com.yadea.pdm.asset;

import java.util.List;
import java.util.Map;
import org.springframework.http.ResponseEntity;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.core.RowMapper;
import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RequestHeader;
import org.springframework.web.bind.annotation.RequestMapping;
import org.springframework.web.bind.annotation.RequestParam;
import org.springframework.web.bind.annotation.RestController;

/**
 * 资产台账 API（Java 侧）。
 *
 * 职责边界（docs/16 §2）：
 *   Java 负责「资产 / 知识 / 集成」上下文——设备台账、备件、维修历史、
 *   与 EAM/MES 的对接。诊断计算在 Python 侧，不在此服务。
 *
 * 多租户：与 Python 侧一致，所有查询必须带 X-Tenant-Id 且与路径/参数一致（D23）。
 */
@RestController
@RequestMapping("/api/v1/assets")
class AssetController {

    private final JdbcTemplate jdbc;

    AssetController(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    private static final RowMapper<Map<String, Object>> ROW =
            (rs, i) -> Map.of(
                    "tenant_id", rs.getString("tenant_id"),
                    "asset_id", rs.getString("asset_id"),
                    "node_id", rs.getString("node_id"),
                    "name", rs.getString("name"),
                    "category", rs.getString("category"),
                    "criticality", rs.getString("criticality"));

    /** 按基地列出设备。 */
    @GetMapping
    List<Map<String, Object>> list(
            @RequestHeader("X-Tenant-Id") String tenant,
            @RequestParam(value = "criticality", required = false) String criticality,
            @RequestParam(value = "limit", defaultValue = "100") int limit) {
        if (criticality != null) {
            return jdbc.query(
                    "SELECT tenant_id, asset_id, node_id, name, category, criticality FROM asset "
                            + "WHERE tenant_id=? AND criticality=? ORDER BY asset_id LIMIT ?",
                    ROW, tenant, criticality, limit);
        }
        return jdbc.query(
                "SELECT tenant_id, asset_id, node_id, name, category, criticality FROM asset "
                        + "WHERE tenant_id=? ORDER BY asset_id LIMIT ?",
                ROW, tenant, limit);
    }

    /** 单台设备详情（含测点）。 */
    @GetMapping("/{assetId}")
    ResponseEntity<Map<String, Object>> get(
            @RequestHeader("X-Tenant-Id") String tenant, @PathVariable String assetId) {
        List<Map<String, Object>> rows = jdbc.query(
                "SELECT tenant_id, asset_id, node_id, name, category, criticality FROM asset "
                        + "WHERE tenant_id=? AND asset_id=?",
                ROW, tenant, assetId);
        if (rows.isEmpty()) {
            return ResponseEntity.notFound().build();
        }
        List<Map<String, Object>> mps = jdbc.queryForList(
                "SELECT mp_id, axis, unit, sample_rate FROM measure_point "
                        + "WHERE tenant_id=? AND mp_id LIKE ? LIMIT 50",
                tenant, "%." + assetId + ".%");
        return ResponseEntity.ok(Map.of(
                "asset", rows.get(0),
                "measure_points", mps));
    }

    /** 关键度分布（看板 KPI 用）。 */
    @GetMapping("/stats/criticality")
    Map<String, Object> criticalityStats(@RequestHeader("X-Tenant-Id") String tenant) {
        List<Map<String, Object>> rows = jdbc.queryForList(
                "SELECT criticality, COUNT(*) AS n FROM asset WHERE tenant_id=? GROUP BY criticality",
                tenant);
        return Map.of("tenant_id", tenant, "distribution", rows);
    }
}
