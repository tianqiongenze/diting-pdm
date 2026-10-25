// k6 压测脚本（docs/43 Phase H3）
//
// 目标：验证 NFR-204（跨基地对标 P95 ≤ 10s）与基础读接口的 P95 基线。
// 运行：
//   k6 run -e BASE=http://host:32231/pdm-api -e TENANT=site-a tools/load_test.js
//
// 判据（thresholds 不达标则 k6 非零退出，可直接接 CI）：
//   - http_req_failed < 1%
//   - fleet 接口 P95 < 10000ms（NFR-204）
//   - overview/alerts P95 < 1500ms（内网首屏）
import http from 'k6/http';
import { check, group, sleep } from 'k6';

const BASE = __ENV.BASE || 'http://10.167.2.175:32231/pdm-api';
const TENANT = __ENV.TENANT || 'site-a';
const HOST = __ENV.HOST_HEADER || 'pdm-api.10.167.2.175.nip.io';

export const options = {
  stages: [
    { duration: '30s', target: 10 },   // 预热
    { duration: '60s', target: 50 },   // 稳态
    { duration: '30s', target: 50 },   // 保持
    { duration: '15s', target: 0 },    // 收尾
  ],
  thresholds: {
    http_req_failed: ['rate<0.01'],
    'http_req_duration{kind:fleet}': ['p(95)<10000'],
    'http_req_duration{kind:read}': ['p(95)<1500'],
  },
};

const headers = { 'X-Tenant-Id': TENANT, Host: HOST };

export default function () {
  group('overview', () => {
    const r = http.get(`${BASE}/v1/stats/overview`, { headers, tags: { kind: 'read' } });
    check(r, { 'overview 200': (x) => x.status === 200 });
  });

  group('alerts', () => {
    const r = http.get(`${BASE}/v1/alerts?limit=50`, { headers, tags: { kind: 'read' } });
    check(r, { 'alerts 200': (x) => x.status === 200 });
  });

  group('fleet-benchmark', () => {
    const r = http.get(`${BASE}/v1/stats/fleet`, { headers, tags: { kind: 'fleet' } });
    check(r, {
      'fleet 200': (x) => x.status === 200,
      'fleet has sites': (x) => {
        try { return Array.isArray(JSON.parse(x.body).sites); } catch (e) { return false; }
      },
    });
  });

  group('world-model', () => {
    const r = http.post(`${BASE}/v1/world/counterfactual`,
      JSON.stringify({ asset_id: 'FAN-00004', horizon_days: 14 }),
      { headers: { ...headers, 'Content-Type': 'application/json' }, tags: { kind: 'read' } });
    check(r, { 'counterfactual 2xx': (x) => x.status >= 200 && x.status < 300 });
  });

  sleep(1);
}
