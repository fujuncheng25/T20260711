# Logistics Group Notifier (Web + PostgreSQL)

一个可直接运行的物流协同网站，核心能力：

1. 将物流总表（CSV/XLS/XLSX）导入 PostgreSQL。
2. 用户注册登录。
3. 用户组管理（如 B组、C组），可添加组成员。
4. 按订单尾号设置提醒，提醒文案可自定义（默认：您的快递到了）。
5. 拍照上传快递面单后，系统 OCR + 一维码扫描识别订单号。
6. 若识别出的订单号命中某组提醒尾号，自动给该组全部成员写入通知。
7. 拍照者会看到该快递归属组；若多个组同时命中，随机显示一个。

## UI 版本号与缓存

本版本 UI 静态资源使用唯一编号：`20260711_200500_94731`

- `static/ui_20260711_200500_94731.css`
- `static/ui_20260711_200500_94731.js`

页面通过 query 参数附加版本号，便于应对 Cloudflare 深缓存。

## 1) 环境准备

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## 2) PostgreSQL 初始化

先在 PostgreSQL 中创建数据库，例如：

```sql
CREATE DATABASE logistics_alert;
```

配置环境变量（PowerShell）：

```powershell
$env:DATABASE_URL="postgresql+psycopg2://postgres:postgres@127.0.0.1:5432/logistics_alert"
$env:SECRET_KEY="replace-with-a-strong-secret"
```

## 3) 启动网站

```bash
python app.py
```

打开：`http://127.0.0.1:5000`

首次启动会自动建表。

## 4) 页面使用流程

1. 注册账号并登录。
2. 在控制台导入物流总表。
3. 创建用户组（例如 B组）。
4. 给组添加成员（成员需先注册）。
5. 在组里创建提醒：输入订单尾号 + 自定义通知文案。
6. 在“拍照识别”页上传快递图片，系统识别后自动通知组成员。
7. 在“通知中心”查看通知，在“物流总表”查看快递与目标组。

## 5) 命令行导入（可选）

你也可以用脚本把表格直接导入数据库：

```bash
python batch_fill.py --input your_logistics.xlsx
```

终端会输出新增/更新/跳过的记录数。

## 6) 关键说明

- 条码识别使用 `zxing-cpp`。
- OCR 使用 `pytesseract`（需要系统安装 Tesseract OCR 可执行程序）。
- 扫描命中规则：识别出的订单号 `endswith(提醒尾号)`。
- 通知为站内通知（写入数据库）。
