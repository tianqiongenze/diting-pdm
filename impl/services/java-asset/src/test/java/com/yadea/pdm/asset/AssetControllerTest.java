package com.yadea.pdm.asset;

import static org.springframework.test.web.servlet.request.MockMvcRequestBuilders.get;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.jsonPath;
import static org.springframework.test.web.servlet.result.MockMvcResultMatchers.status;

import org.junit.jupiter.api.Test;
import org.springframework.beans.factory.annotation.Autowired;
import org.springframework.boot.test.autoconfigure.web.servlet.AutoConfigureMockMvc;
import org.springframework.boot.test.context.SpringBootTest;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.test.web.servlet.MockMvc;

/**
 * 资产 API 测试：多租户隔离 + 关键度统计。
 *
 * 依赖测试库（需可连 TiDB）。CI 中由 testcontainers 或专用 test 库提供；
 * 本地未配数据库时自动跳过。
 */
@SpringBootTest
@AutoConfigureMockMvc
class AssetControllerTest {

    @Autowired
    private MockMvc mvc;

    @Autowired
    private JdbcTemplate jdbc;

    private boolean dbUp() {
        try {
            jdbc.queryForObject("SELECT 1", Integer.class);
            return true;
        } catch (Exception e) {
            return false;
        }
    }

    @Test
    void listAssetsRequiresTenant() throws Exception {
        org.junit.jupiter.api.Assumptions.assumeTrue(dbUp(), "数据库不可达，跳过");
        mvc.perform(get("/api/v1/assets").header("X-Tenant-Id", "site-a").param("limit", "5"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$").isArray());
    }

    @Test
    void tenantIsolation() throws Exception {
        org.junit.jupiter.api.Assumptions.assumeTrue(dbUp(), "数据库不可达，跳过");
        mvc.perform(get("/api/v1/assets").header("X-Tenant-Id", "site-b").param("limit", "5"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$[0].tenant_id").value("site-b"));
    }

    @Test
    void criticalityStats() throws Exception {
        org.junit.jupiter.api.Assumptions.assumeTrue(dbUp(), "数据库不可达，跳过");
        mvc.perform(get("/api/v1/assets/stats/criticality").header("X-Tenant-Id", "site-a"))
                .andExpect(status().isOk())
                .andExpect(jsonPath("$.tenant_id").value("site-a"))
                .andExpect(jsonPath("$.distribution").isArray());
    }

    @Test
    void unknownAssetReturns404() throws Exception {
        org.junit.jupiter.api.Assumptions.assumeTrue(dbUp(), "数据库不可达，跳过");
        mvc.perform(get("/api/v1/assets/NOPE-999").header("X-Tenant-Id", "site-a"))
                .andExpect(status().isNotFound());
    }
}
