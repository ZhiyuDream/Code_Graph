# 将 clangd 升级到 20（用于 Code_Graph 的 CALLS 采集）

当前若为 Ubuntu 自带的 clangd 14，`run_stage1_clangd.py` 无法采集调用边（CALLS=0）。  
按下面任选一种方式安装 **clangd 20**，并在本机**自行执行**所列命令。

---

## 方式一：使用 LLVM 官方安装脚本（推荐）

在终端依次执行：

```bash
# 下载并运行官方脚本，安装 LLVM 20（含 clangd-20）
wget https://apt.llvm.org/llvm.sh
chmod +x llvm.sh
sudo ./llvm.sh 20
```

安装后会有 **`clangd-20`**（不会覆盖系统的 `clangd`）。若希望默认用 20，可设别名或改 PATH（见文末「让脚本用 clangd-20」）。

---

## 方式二：手动添加 apt 源后安装

根据你的 **Ubuntu 版本** 选择对应源（以下为常见 LTS）：

**Ubuntu 22.04 (Jammy)：**
```bash
wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key | sudo tee /etc/apt/trusted.gpg.d/apt.llvm.org.asc
sudo add-apt-repository "deb http://apt.llvm.org/jammy/ llvm-toolchain-jammy-20 main"
sudo apt-get update
sudo apt-get install -y clangd-20
```

**Ubuntu 24.04 (Noble)：**
```bash
wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key | sudo tee /etc/apt/trusted.gpg.d/apt.llvm.org.asc
sudo add-apt-repository "deb http://apt.llvm.org/noble/ llvm-toolchain-noble-20 main"
sudo apt-get update
sudo apt-get install -y clangd-20
```

**Ubuntu 20.04 (Focal)：**
```bash
wget -qO- https://apt.llvm.org/llvm-snapshot.gpg.key | sudo tee /etc/apt/trusted.gpg.d/apt.llvm.org.asc
sudo add-apt-repository "deb http://apt.llvm.org/focal/ llvm-toolchain-focal-20 main"
sudo apt-get update
sudo apt-get install -y clangd-20
```

更多发行版见：<https://apt.llvm.org/>

---

## 安装后确认

```bash
clangd-20 --version
```

应看到版本号含 20.x。

---

## 让 run_stage1_clangd.py 使用 clangd-20

**脚本已支持自动优先使用 clangd-20**：只要本机 PATH 里能找到 `clangd-20`，`run_stage1_clangd.py` 会用它，无需改系统默认 `clangd`。安装完 clangd-20 后直接运行即可：

```bash
cd /data/yulin/RUC/Code_Graph
python run_stage1_clangd.py
```

若安装后不在默认 PATH（例如装在 `/usr/lib/llvm-20/bin/clangd`），可设置环境变量指定可执行文件：

```bash
export CODEGRAPH_CLANGD_CMD=/usr/lib/llvm-20/bin/clangd
python run_stage1_clangd.py
```

---

*以上命令均需你在本机终端执行；安装与 alternatives 会涉及 sudo。*
