.PHONY: e2e e2e-d9 worker obs-up obs-down obs-logs obs-test

worker: ## 启动 ai-service 的 Arq worker（消费蒸馏队列）
	@REPO_ROOT="$$(git rev-parse --show-toplevel)"; \
	  PYTHONPATH="$$(dirname "$$REPO_ROOT"):$$REPO_ROOT/backend/ai-service:$${PYTHONPATH}" \
	  backend/.venv/bin/arq worker.WorkerSettings

e2e-d9: ## D9 端到端集成测（CP1.7.2）：需先起 4 服务 bash backend/run_dev.sh
	@bash tests/e2e/d9_flow.sh

e2e: e2e-d9 ## 跑所有 E2E
	@echo "✅ All E2E passed"

# ---- Observability（CP6.4-pre-2：Prometheus + Grafana + Alertmanager）----
# ⚠️ 顺序：先起 4 服务（bash backend/run_dev.sh）再 make obs-up，否则 scrape target 全是 DOWN
OBS_COMPOSE = docker compose -f infra/docker/docker-compose.observability.yml

obs-up: ## 启动 Prometheus(9090) + Grafana(3000) + Alertmanager(9093)
	@$(OBS_COMPOSE) up -d
	@echo "Prometheus  http://localhost:9090/targets | Grafana http://localhost:3000 (admin/stashbox_dev) | Alertmanager http://localhost:9093"

obs-down: ## 停 observability 容器（volume 保留）
	@$(OBS_COMPOSE) down

obs-logs: ## 看 observability 日志
	@$(OBS_COMPOSE) logs -f

obs-test: ## 校验 observability 配置（YAML / PromQL / dashboard JSON）
	@PYTHONPATH="$$(git rev-parse --show-toplevel)":"$$(git rev-parse --show-toplevel)/.." \
	  backend/.venv/bin/python -m pytest infra/docker/tests/ -q
