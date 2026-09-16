.PHONY: e2e e2e-d9 worker

worker: ## 启动 ai-service 的 Arq worker（消费蒸馏队列）
	@REPO_ROOT="$$(git rev-parse --show-toplevel)"; \
	  PYTHONPATH="$$(dirname "$$REPO_ROOT"):$$REPO_ROOT/backend/ai-service:$${PYTHONPATH}" \
	  backend/.venv/bin/arq worker.WorkerSettings

e2e-d9: ## D9 端到端集成测（CP1.7.2）：需先起 4 服务 bash backend/run_dev.sh
	@bash tests/e2e/d9_flow.sh

e2e: e2e-d9 ## 跑所有 E2E
	@echo "✅ All E2E passed"
