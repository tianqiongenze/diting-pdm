.PHONY: help venv install test test-verbose lint fmt run build docker k8s-dry schema-check clean

PY      := .venv/Scripts/python.exe
PYTHONPATH_ENV := packages/pdm_signal;services/python-diagnosis

help:
	@echo "Diting PdM · 常用命令"
	@echo "  make venv        创建虚拟环境"
	@echo "  make install     安装依赖"
	@echo "  make test        运行全部测试"
	@echo "  make lint        静态检查（ruff）"
	@echo "  make run         本地启动诊断服务（:8000）"
	@echo "  make smoke       冒烟测试（需先 make run）"
	@echo "  make k8s-dry     校验 K8s 清单（dry-run）"
	@echo "  make clean       清理缓存"

venv:
	python -m venv .venv

install: venv
	$(PY) -m pip install -q --upgrade pip
	$(PY) -m pip install -q -r services/python-diagnosis/requirements.txt
	$(PY) -m pip install -q ruff

test:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest -q

test-verbose:
	PYTHONPATH="$(PYTHONPATH_ENV)" $(PY) -m pytest -v

lint:
	$(PY) -m ruff check packages services

fmt:
	$(PY) -m ruff format packages services

run:
	cd services/python-diagnosis && PYTHONPATH="../../packages/pdm_signal;." ../../$(PY) -m granian --interface asgi app.server:app --host 0.0.0.0 --port 8000 --reload

smoke:
	curl -s http://127.0.0.1:8000/healthz
	@echo ""
	curl -s -X POST http://127.0.0.1:8000/v1/diagnose \
	  -H "Content-Type: application/json" -H "X-Tenant-Id: site-a" \
	  -d '{"tenant_id":"site-a","asset_id":"KA-101","metrics":{"vib_rms_mm_s":4.0,"vib_baseline_mm_s":2.0,"kurtosis":6.5}}'

k8s-dry:
	kubectl apply --dry-run=client -f deploy/k8s/pdm-prod.yaml

schema-check:
	@echo "需要 TiDB 连接后执行：mysql -h <host> -P 4000 -u <user> -p pdm < db/tidb/001_schema.sql"
	@echo "本命令仅做语法粗检（是否存在明显占位符）"
	@grep -n "TODO\|FIXME\|CHANGEME" db/tidb/001_schema.sql || echo "schema ok"

docker:
	docker build -t pdm/python-diagnosis:0.1.0 -f services/python-diagnosis/Dockerfile .

clean:
	rm -rf .pytest_cache .ruff_cache **/__pycache__ **/*.pyc
