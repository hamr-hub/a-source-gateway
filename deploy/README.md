# 部署手册(a-source-gateway)

三种部署方式,按场景选一种:

| 方式 | 场景 | 文件 |
| --- | --- | --- |
| Docker Compose | 本机 / 单机 / 开发验证 | [`../docker-compose.yml`](../docker-compose.yml) |
| systemd | 单台长期运行的服务器 | [`systemd/a-source-gateway.service`](systemd/a-source-gateway.service) |
| Kubernetes | 集群、多副本 | [`k8s/`](k8s/) |

---

## 0. 先记住三件事

1. **改的是 `data/` 就得重启**(注册表在进程启动时从 `data/` 构建)。
   Compose 场景要重新 `build`(镜像里的 `data/` 是烤进去的);
   systemd / k8s 场景重启即可(数据文件随包安装)。
2. **缓存是进程内的**(in-process)。不落盘、不跨副本共享。
   重启 = 冷缓存;k8s 里两个副本各有一份缓存。想持久化缓存是改网关实现,不是加 volume。
3. **`A_GATEWAY_CONTACT` 强烈建议设成真实邮箱。** 它写进 `User-Agent`,
   SEC、OpenAlex 等上游据此识别调用方,不带邮箱 SEC 直接 403。
   它是联系邮箱,不是密钥。

---

## 1. Docker Compose

### 1.1 安装

```bash
git clone https://github.com/hamr-hub/a-source-gateway.git
cd a-source-gateway
docker compose build          # 首次要拉基础镜像 + 装 Python 依赖,几分钟
```

### 1.2 配置

```bash
cp .env.example .env
$EDITOR .env          # 至少把 A_GATEWAY_CONTACT 改成真实邮箱
```

也可以不建 `.env`,直接在命令前面带环境变量:

```bash
A_GATEWAY_CONTACT=you@example.com docker compose up -d
```

常用变量(全部有默认值):

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `PORT` | `8848` | 宿主机端口(容器内永远是 8848) |
| `A_GATEWAY_CONTACT` | 空 | 联系邮箱,写进 UA |
| `A_GATEWAY_BUDGET_MS` | `500` | 单次调用总预算 |
| `A_GATEWAY_CACHE_ENTRIES` | `512` | 缓存条目上限 |
| `A_GATEWAY_CACHE_BYTES` | `33554432` | 缓存字节上限 |
| `A_GATEWAY_PREWARM` | `1` | 启动预热 |
| `A_GATEWAY_PARSER` | `auto` | `auto` / `python` / `go` |
| `A_GATEWAY_HTTP2` | `1` | 上游走 HTTP/2;个别上游 h2 有问题时设 `0` |

### 1.3 启动

```bash
docker compose up -d --build     # 改了 data/ 或代码后用这个
docker compose ps                # 看 STATUS,应该是 "healthy"
docker compose logs -f gateway   # 看日志
```

一次性 CLI(默认不启动,用 profile 拉起来):

```bash
docker compose --profile tools run --rm tools list
docker compose --profile tools run --rm tools validate
```

### 1.4 验证

```bash
curl -s http://127.0.0.1:8848/livez   | python3 -m json.tool   # {"ok":true,"uptime_s":...}
curl -s http://127.0.0.1:8848/readyz  | python3 -m json.tool   # {"ok":true,"sources":97,...}
curl -s "http://127.0.0.1:8848/v1/sources?q=制裁" | python3 -m json.tool
```

`/livez` = 进程活着;`/readyz` = 注册表已加载、能接流量。

### 1.5 回滚

```bash
# 1) 回到上一个镜像
docker compose down
git checkout <上一个好版本>
docker compose up -d --build

# 2) 只回滚配置(改错了 env)
docker compose down && $EDITOR .env && docker compose up -d
```

镜像本身没有历史版本标签时(`a-source-gateway:local` 是本地构建),
回滚就是回到上一个能用的 git 版本重新 build。

---

## 2. systemd

### 2.1 安装

```bash
# 二进制:从 Release 下载,或本地构建
make gateway-build                     # → bin/a-source-gateway
sudo install -m 0755 bin/a-source-gateway /usr/local/bin/a-source-gateway

# Python 解析面(网关会拉起 python3 -m a_source_gateway.parser_worker)
# 系统 Python 3.10+ 即可;用发行包或 pip 装都行:
pip3 install a-source-gateway        # 装进系统 Python
# 或从源码装(仓库根):
pip3 install .

# 服务账号与目录
sudo useradd --system --home-dir /var/lib/a-source-gateway --shell /usr/sbin/nologin a-source-gateway
sudo install -d -m 0750 -o root -g a-source-gateway /etc/a-source-gateway
sudo install -d -m 0750 -o a-source-gateway -g a-source-gateway /var/lib/a-source-gateway

# 事实源(data/):Go 网关从工作目录和可执行文件目录逐级向上找 data/,
# 装到 /var/lib/a-source-gateway/data 就正好落在 systemd 的 WorkingDirectory 下。
sudo install -d -m 0750 -o a-source-gateway -g a-source-gateway /var/lib/a-source-gateway/data
sudo install -m 0640 -o a-source-gateway -g a-source-gateway data/source_inventory.csv \
     /var/lib/a-source-gateway/data/
sudo install -m 0640 -o a-source-gateway -g a-source-gateway data/source_inventory_cn.csv \
     /var/lib/a-source-gateway/data/
sudo install -m 0640 -o a-source-gateway -g a-source-gateway data/endpoints.json \
     /var/lib/a-source-gateway/data/
sudo install -m 0640 -o a-source-gateway -g a-source-gateway data/aliases.json \
     /var/lib/a-source-gateway/data/

# 单元文件
sudo install -m 0644 deploy/systemd/a-source-gateway.service /etc/systemd/system/a-source-gateway.service
sudo systemctl daemon-reload
```

> 想跳过 Python:装 `A_GATEWAY_PARSER=go`(见下文配置),这时只需要 Go 二进制。
> 更新事实源:重新 `install` 那三个文件,然后 `systemctl restart`。

### 2.2 配置

```bash
sudo install -m 0640 -o root -g a-source-gateway \
     deploy/systemd/env.example /etc/a-source-gateway/env
sudo editor /etc/a-source-gateway/env
```

`EnvironmentFile=-/etc/a-source-gateway/env` 里的 `-` 表示文件缺失也不报错。

### 2.3 启动

```bash
sudo systemctl enable --now a-source-gateway
sudo systemctl status a-source-gateway
sudo journalctl -u a-source-gateway -f
```

### 2.4 验证

```bash
curl -s http://127.0.0.1:8848/livez
curl -s http://127.0.0.1:8848/readyz
systemctl is-active a-source-gateway     # active
```

### 2.5 回滚

```bash
# 换回旧二进制
sudo install -m 0755 /path/to/previous/a-source-gateway /usr/local/bin/a-source-gateway
sudo systemctl restart a-source-gateway

# 配置改错了
sudo cp /etc/a-source-gateway/env.bak /etc/a-source-gateway/env
sudo systemctl restart a-source-gateway
```

旧的二进制要自己留一份 —— systemd 单元不会自动保留上一个版本。

---

## 3. Kubernetes

### 3.1 apply 顺序

```bash
kubectl create namespace a-source-gateway          # 一次
kubectl apply -k deploy/k8s/                       # ConfigMap → Deployment → Service
```

顺序由 [`k8s/kustomization.yaml`](k8s/kustomization.yaml) 的 `resources` 列表决定:
先 ConfigMap(参数),再 Deployment(工作负载),最后 Service(流量入口)。
先有 Deployment 再有 Service,避免流量打到一个还不存在的选择器上。

凭据单独建(不进仓库):

```bash
kubectl -n a-source-gateway create secret generic a-source-gateway-secrets \
  --from-literal=A_GATEWAY_<某源>_KEY='<value>'
```

然后放开 [`k8s/deployment.yaml`](k8s/deployment.yaml) 里 `secretRef` 那段注释。
形状见 [`k8s/secret.example.yaml`](k8s/secret.example.yaml)(整份是注释,不生效)。

### 3.2 验证

```bash
kubectl -n a-source-gateway rollout status deployment/a-source-gateway
kubectl -n a-source-gateway get pods -o wide
kubectl -n a-source-gateway port-forward svc/a-source-gateway 8848:8848
curl -s http://127.0.0.1:8848/livez
```

### 3.3 回滚

```bash
# 回滚到上一个 ReplicaSet
kubectl -n a-source-gateway rollout undo deployment/a-source-gateway
kubectl -n a-source-gateway rollout history deployment/a-source-gateway

# 镜像回滚(改完重新 build / push 后)
kubectl -n a-source-gateway set image deployment/a-source-gateway \
  gateway=ghcr.io/hamr-hub/a-source-gateway:<上一个可用 tag>
```

滚动策略是 `maxUnavailable: 0 / maxSurge: 1`,所以更新期间不会掉到 0 副本;
`preStop` 里的 `sleep 5` 让 Pod 先从 Endpoints 摘掉再收 SIGTERM,在途请求不会被打断。

---

## 4. 镜像本身

```bash
docker build -t a-source-gateway:local .    # 多阶段:Go 编译 → python:3.12-slim 运行时
```

镜像里有三个入口:

| 路径 | 是什么 |
| --- | --- |
| `/usr/local/bin/a-source-gateway` | Go 网关(容器 ENTRYPOINT) |
| `/usr/local/bin/a-source-gateway-cli` | Python CLI(`list` / `validate` / `bench` / `show`) |
| `/usr/local/bin/healthcheck.py` | 容器健康检查(打 `/livez`) |

> Python 包原本也装出一个叫 `a-source-gateway` 的命令,会和 Go 二进制撞名,
> 所以镜像里把它改名成了 `a-source-gateway-cli`。

非 root 运行(uid/gid 10001),和 k8s 的 `runAsUser: 10001` 对齐。

---

## 5. 排障

| 症状 | 先查 |
| --- | --- |
| `/livez` 不通 | `docker compose logs gateway` / `journalctl -u a-source-gateway`;容器起不来多半是数据文件没找到 |
| `/readyz` 报 sources 为 0 | `data/` 没进镜像 → 重新 `build`;或 `A_GATEWAY_DATA_DIR` 指错了 |
| 上游 403 | `A_GATEWAY_CONTACT` 没设或不是有效邮箱 |
| 响应带 `degraded` | 看 `meta.errors[]` 与 `meta.fallback.upstream_url`,里面直接写了原因和下一步 |
| 超时 / `budget_exceeded` | 端点 `budget_ms` 与上游实际延迟;必要时调小 `page_size` |
| Python worker 起不来 | `A_GATEWAY_PARSER=go` 可以先顶上(纯 Go 解析);`which python3`、包是否装在系统 Python 里 |
