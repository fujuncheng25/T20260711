# Tmall CSV Auto Filler

一个手写 Web UI（Flask + HTML）工具，功能如下：

1. 上传 CSV 文件。
2. 读取 `订单号` 列。
3. 复用你当前 Firefox 登录态访问天猫订单详情页（不新开浏览器）。
4. 抓取 `快递单号` / `到达时间` / `物品简述` / `付款时间`（多策略提取）。
5. 将抓到的 `付款时间` 自动补到 `购买时间` 列（仅当该单元格为空时）。
6. 仅补全空白单元格，不覆盖原有内容。
7. 提供补全后 CSV 下载按钮。

## 环境准备

```bash
pip install -r requirements.txt
```

## 启动

```bash
python app.py
```

打开浏览器访问：`http://127.0.0.1:5000`

## 使用建议

- 若你已经在 Firefox 登录了淘宝/天猫，建议在侧边栏填写 Firefox Profile 路径，以复用登录态。
- 程序不会新开 Firefox 窗口，会直接读取当前 Firefox 的 Cookie（可手动指定 Profile 路径）。
- 如果抓取不到某条订单信息，该行会保持原值不变。
- 程序只会写入空白值（空字符串、`nan`、`/`、`\\` 等会视为空）。

## Firefox Profile 路径示例

`C:/Users/<用户名>/AppData/Roaming/Mozilla/Firefox/Profiles/xxxx.default-release`
