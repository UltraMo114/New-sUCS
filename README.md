# New sUCS（log-version）

本仓库主要是脚本型项目（非打包发布），推荐使用 `uv` 管理虚拟环境与依赖。

## 1) 创建虚拟环境

```bash
uv venv
```

激活（macOS / Linux）：

```bash
source .venv/bin/activate
```

## 2) 安装运行依赖

```bash
uv sync
```

## 3) 安装测试环境依赖（含 pytest）

```bash
uv sync --extra test
```

## 4) 运行测试

```bash
uv run pytest
```

