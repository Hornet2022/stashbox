# StashBox 监控基础设施

## 启动方式

```bash
cd infra
docker compose up -d
```

## 服务端口

- Prometheus: http://localhost:9090
- Alertmanager: http://localhost:9093
- Grafana: http://localhost:3000 (默认账号 admin/admin)

## 配置说明

- `prometheus/prometheus.yml` - Prometheus 抓取配置
- `alertmanager/alertmanager.yml` - Alertmanager 告警路由配置
- `grafana/dashboards/api.json` - Grafana 仪表盘
