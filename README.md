# Logistics Photo Log + Auto Reminder (Flask + SQLite)

这个版本严格只保留两个功能：

1. 拍照记录物流：上传面单照片时记录拍照上传人，并写入当日日志。
2. 自动提醒：当识别到的快递单号命中已设置的尾号规则时，生成提醒；提醒中嵌入照片和拍照人。

不再包含登录、分组、注册等流程。

## OCR 升级

当前识别链路是多模型融合，而不是单一 OCR：

- 条码识别：zxing-cpp
- OCR 模型 1：RapidOCR (ONNX Runtime)
- OCR 模型 2：EasyOCR (PyTorch)

系统会融合多路结果后再提取候选单号，并做尾号匹配。

## 运行环境

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 启动

```bash
python app.py
```

默认监听端口：`5000`。

可选环境变量（PowerShell）：

```powershell
$env:PORT="5000"
$env:SECRET_KEY="replace-with-a-strong-secret"
$env:SQLITE_DB_PATH="C:\path\to\logistics_alert.db"
```

## 页面功能

- 首页 `/`
  - 上传照片 + 填写拍照人（写入今日日志）
  - 创建/启用/停用尾号提醒规则
  - 查看今日日志（含图片、拍照人、识别结果）
  - 查看自动提醒记录（提醒内含图片和拍照人）

- 上传文件访问 `/uploads/<filename>`

- 管理入口（仅本机 loopback 可访问）
  - `/admin/`
  - `/admin/database/download`
  - `/admin/database/upload`

## 管理入口规则

- 只有从本机 `127.0.0.1` / `::1` 发起请求时，`/admin/*` 才可访问。
- 局域网、Tailscale、ZeroTier、公网来源访问 `/admin/*` 均返回 `404`。
- 管理页支持下载/上传并覆盖 SQLite 文件，覆盖前自动备份并做完整性校验。

## 批量导入脚本

`batch_fill.py` 现在用于批量导入“提醒规则”，而不是物流总表。

```bash
python batch_fill.py --input reminders.xlsx
```

输入文件至少包含两列（大小写不敏感）：

- `watcher_name`
- `order_suffix`

可选列：

- `custom_message`
- `is_active`

## 说明

- 数据库是 SQLite 单文件，无需 PostgreSQL。
- OCR 仅依赖 Python 包，不依赖外部系统可执行程序。
- EasyOCR 首次运行会下载模型权重，后续离线可复用缓存。
