# OpenClaw 部署方案对比

## 方案总览

| 方案 | 隔离级别 | 编排 | 复杂度 | 成本 | 适用场景 |
|------|---------|------|--------|------|---------|
| **A. EC2 + Firecracker (当前)** | microVM | ASG + Lambda | 中 | 低 | 生产首选，已验证 |
| **B. EKS + Kata Containers** | microVM | K8s | 高 | 中 | K8s 原生团队 |
| **C. EKS + Privileged Pod** | microVM | K8s | 中 | 中 | 快速迁移 |
| **D. EKS + gVisor** | 内核沙箱 | K8s | 低 | 低 | 轻量隔离够用时 |
| **E. AgentCore Runtime** | microVM | 全托管 | 最低 | 按量 | 纯 Serverless |

---

## 方案 A: EC2 + Firecracker（当前方案）✅ 已验证

```
用户 → API Gateway → Lambda → SSM → EC2 Host → Firecracker → microVM (OpenClaw)
                                      ↑ ASG 自动扩缩
ALB → Nginx:80 → VM Gateway:18789
```

### 架构
- EC2 实例（c8i/m8i/r8i）开启嵌套虚拟化
- 每台宿主机跑多个 Firecracker microVM
- Lambda + SSM 管理 VM 生命周期
- ALB path-based routing 暴露 Dashboard

### 优势
- ✅ 已在生产验证（v0.9.1）
- ✅ microVM 级隔离（独立内核）
- ✅ 一键部署（CDK）
- ✅ AgentCore 集成
- ✅ 成本低（高密度 + Spot 支持）
- ✅ 全部代码开源

### 劣势
- ❌ 非 K8s 原生（SSM 管理而非 kubectl）
- ❌ 仅支持 Intel 实例
- ❌ 自定义编排逻辑（Lambda + DynamoDB）

### 适用
- 中小规模（1-100 租户）
- 需要 microVM 级隔离
- 团队不强制 K8s

---

## 方案 B: EKS + Kata Containers + Firecracker

```
用户 → K8s API → EKS → Worker Node (嵌套虚拟化) → Kata Runtime → Firecracker → Pod = microVM
                        ↑ Karpenter/CA 自动扩缩
ALB Ingress Controller → Pod:18789
```

### 架构
- EKS 集群，Worker Node 用 c8i/m8i/r8i（嵌套虚拟化）
- Kata Containers 作为 CRI 运行时，底层用 Firecracker
- 每个 Pod = 一个 Firecracker microVM = 一个 OpenClaw 实例
- K8s Ingress 替代 ALB path-based routing

### 改造工作

| 当前组件 | 改造为 | 工作量 |
|---------|--------|--------|
| ASG | EKS Node Group (c8i) | 中 |
| Lambda + SSM | K8s API + kubectl | 大 |
| DynamoDB (tenants/hosts) | K8s CRD 或保留 DynamoDB | 中 |
| ALB + Nginx | ALB Ingress Controller | 中 |
| Health Check Lambda | K8s liveness/readiness probe | 小 |
| Scaler Lambda | Karpenter / HPA | 中 |
| Backup Lambda | K8s CronJob | 小 |
| init-host.sh | DaemonSet + init container | 中 |
| launch-vm.sh | Pod spec (Kata runtime class) | 大 |

### 优势
- ✅ K8s 原生（kubectl, helm, ArgoCD）
- ✅ microVM 级隔离（Kata + Firecracker）
- ✅ 标准 K8s 生态（Prometheus, Grafana, Istio）
- ✅ 滚动更新、蓝绿部署

### 劣势
- ❌ Kata Containers 安装配置复杂
- ❌ EKS 控制面费用（~$73/月）
- ❌ Worker Node 必须支持嵌套虚拟化
- ❌ 改造工作量大（~2-3 周）
- ❌ Kata + Firecracker 在 EKS 上不是官方支持路径

### 适用
- 团队已有 K8s 运维能力
- 需要与现有 K8s 工作负载混部
- 对 K8s 生态有强依赖

### 关键配置

```yaml
# RuntimeClass for Kata + Firecracker
apiVersion: node.k8s.io/v1
kind: RuntimeClass
metadata:
  name: kata-fc
handler: kata-fc

---
# OpenClaw Pod
apiVersion: v1
kind: Pod
metadata:
  name: openclaw-tenant-alice
spec:
  runtimeClassName: kata-fc  # 用 Kata + Firecracker
  containers:
  - name: openclaw
    image: openclaw-rootfs:v1.1
    ports:
    - containerPort: 18789
    resources:
      limits:
        cpu: "1"
        memory: 2Gi
    volumeMounts:
    - name: data
      mountPath: /home/agent
  volumes:
  - name: data
    persistentVolumeClaim:
      claimName: openclaw-alice-data
```

---

## 方案 C: EKS + Privileged Pod + Firecracker

```
用户 → K8s API → EKS → Worker Node → Privileged Pod → /dev/kvm → Firecracker → microVM
```

### 架构
- EKS Worker Node 开启嵌套虚拟化
- 每个 Pod 是 privileged 的，挂载 /dev/kvm
- Pod 内部运行 Firecracker，启动 microVM
- 类似当前方案，但用 K8s 替代 ASG + Lambda

### 改造工作
- 比方案 B 简单（不需要 Kata）
- 把 launch-vm.sh 逻辑放到 Pod 的 entrypoint 里
- K8s Service/Ingress 替代 ALB

### 优势
- ✅ 改造量比方案 B 小
- ✅ 保留 Firecracker microVM 隔离
- ✅ K8s 管理

### 劣势
- ❌ Privileged Pod 安全风险高
- ❌ Pod 内嵌套 Firecracker，调试复杂
- ❌ 不是 K8s 最佳实践
- ❌ 仍需嵌套虚拟化实例

### 适用
- 快速 PoC，验证 EKS 可行性
- 不推荐生产使用

---

## 方案 D: EKS + gVisor（降级隔离）

```
用户 → K8s API → EKS → Worker Node → gVisor Runtime → Container (OpenClaw)
```

### 架构
- 用 gVisor (runsc) 替代 Firecracker
- gVisor 在用户态拦截系统调用，不需要 KVM
- 隔离级别低于 microVM，但高于普通容器
- 不需要嵌套虚拟化，任何 EC2 实例都行

### 优势
- ✅ 不需要嵌套虚拟化（任何实例类型）
- ✅ K8s 原生 RuntimeClass
- ✅ 改造量最小
- ✅ GKE 原生支持，EKS 可配置

### 劣势
- ❌ 隔离级别低于 microVM（共享内核）
- ❌ 系统调用兼容性问题（部分 syscall 不支持）
- ❌ 性能开销（syscall 拦截）

### 适用
- 隔离要求不高（内部使用）
- 不想受限于特定实例类型
- 快速上 K8s

---

## 方案 E: AgentCore Runtime（全托管）

```
用户 → AgentCore API → AgentCore Runtime → microVM (Agent)
                        ↑ 全托管，零运维
```

### 架构
- 直接用 AgentCore Runtime 部署 Agent
- 每个 Agent 会话跑在独立 microVM 里
- 支持任意框架（OpenClaw/Strands/LangGraph）
- Gateway + Memory + Code Interpreter + Browser 全托管

### 优势
- ✅ 零运维（无 EC2、无 EKS）
- ✅ 自动扩缩（0 到数百会话）
- ✅ 原生 MCP 支持（有状态会话）
- ✅ 内置 Memory/Gateway/Identity

### 劣势
- ❌ 无法自定义 rootfs（不能预装工具链）
- ❌ 会话最长 8 小时（不适合长期运行）
- ❌ 无持久化数据盘
- ❌ 按量计费，大规模可能比 EC2 贵
- ❌ 无 Dashboard 直达

### 适用
- 纯 Serverless 场景
- 短时任务（对话、代码执行）
- 不需要持久化 Agent 实例

---

## 方案对比矩阵

| 维度 | A. EC2+FC | B. EKS+Kata | C. EKS+Priv | D. EKS+gVisor | E. AgentCore |
|------|-----------|-------------|-------------|----------------|-------------|
| 隔离级别 | ⭐⭐⭐ microVM | ⭐⭐⭐ microVM | ⭐⭐⭐ microVM | ⭐⭐ 内核沙箱 | ⭐⭐⭐ microVM |
| K8s 原生 | ❌ | ✅ | ⚠️ | ✅ | ❌ |
| 运维复杂度 | 中 | 高 | 中 | 低 | 最低 |
| 改造工作量 | 0（当前） | 2-3 周 | 1-2 周 | 1 周 | 1-2 周 |
| 实例限制 | Intel only | Intel only | Intel only | 无限制 | 无（托管） |
| 持久化数据 | ✅ | ✅ PVC | ✅ PVC | ✅ PVC | ❌ |
| 长期运行 | ✅ | ✅ | ✅ | ✅ | ❌ 8h 上限 |
| 成本 | 低 | 中 | 中 | 低 | 按量 |
| AgentCore | ✅ 已集成 | ✅ 可集成 | ✅ 可集成 | ✅ 可集成 | ✅ 原生 |
| 生产就绪 | ✅ | ⚠️ 需验证 | ❌ | ⚠️ | ✅ |

---

## 推荐路径

```
当前: 方案 A (EC2 + Firecracker) — 已验证，生产可用
  │
  ├── 客户要求 K8s → 方案 B (EKS + Kata) — 需 2-3 周改造
  │
  ├── 快速验证 K8s → 方案 C (Privileged Pod) — PoC only
  │
  ├── 隔离要求低 → 方案 D (gVisor) — 最快上 K8s
  │
  └── 纯 Serverless → 方案 E (AgentCore Runtime) — 零运维
```

### 我的建议

1. **短期**：继续用方案 A，已经验证稳定
2. **中期**：如果客户强需 K8s，走方案 B（Kata + Firecracker），但需要投入 2-3 周做改造和验证
3. **长期**：关注 AgentCore Runtime 的演进，如果支持自定义 rootfs + 持久化，可以考虑方案 E 替代自建

### EKS 改造的核心问题

能不能改？**能**。但要回答一个关键问题：

> 客户要的是"跑在 EKS 上"还是"用 K8s 管理"？

如果是后者，可以用 **EKS + 方案 A 混合**：EKS 管理控制面（API/Lambda 容器化），EC2 Node Group（嵌套虚拟化）跑 Firecracker。这样既有 K8s 管理体验，又保留 microVM 隔离，改造量最小。
