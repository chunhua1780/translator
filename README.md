# PDF 说人话翻译

这个仓库里有两个版本，功能一样：把 PDF（以及 Word、TXT）翻译或改写成通俗好懂的“说人话”版本，排版不变，最后拼成完整的 PDF。

- **网页版（不用安装，iPad 也能用）**：<https://chunhua1780.github.io/translator/>
- **本地 Docker 版**：在自己电脑上运行，适合整本的大手册，见下面“本地 Docker 版”。

---

## 网页版
把 PDF、Word (.docx) 或 TXT 文件翻译成中文/英文，或者改写成通俗好懂的“说人话”版本，并按原来的排版生成新的 PDF。

### 怎么用

1. 打开网站，填一次 Anthropic API 密钥（在 [console.anthropic.com](https://console.anthropic.com/settings/keys) 创建，按用量付费）。密钥只保存在这台设备的浏览器里。
2. 选择文件（最大 100 MB），选好目标语言、风格和模型，点“开始处理”。会先显示预计费用。
3. 网站在后台一页一页处理，做完的页自动保存。关掉网页再打开，点“继续”就从断点接着做。
4. 完成后点“下载 PDF”，也可以“预览”对照原文，或下载纯文字版。

### 工作原理

- 全部在浏览器里运行，网站本身不需要服务器，可以直接放在 GitHub Pages 上。
- PDF：用 pdf.js 读出每页文字的位置，按段落分块，每页发给 Claude（Opus 5.5 或 Sonnet 5.5）改写，返回结构化 JSON；再用 pdf-lib 在原来的位置盖上底色、写入新文字。图片、表格线、背景都保持原样，新文字是真正的文字（可以选中、搜索）。
- 扫描件（没有文字层的页面）会把页面图片发给 Claude 识别文字位置。
- 字体：嵌入 Noto Sans SC / TC，并用 HarfBuzz 只保留用到的字，所以生成的文件很小。
- Word / TXT 没有固定排版，会生成一份排版整洁的新 PDF。
- 进度和结果保存在浏览器的 IndexedDB 里。

### 文件

- `index.html`, `style.css`, `app.js` — 网页版（本地 Docker 版的代码在 `app/` 里）
- `fonts/` — Noto Sans SC/TC（SIL Open Font License，见 `fonts/OFL.txt`）
- `vendor/hb-subset.wasm` — HarfBuzz 字体子集工具（harfbuzzjs，MIT 许可，见 `vendor/HARFBUZZJS-LICENSE.txt`）

---

## 本地 Docker 版
把航空公司手册（中文或英文 PDF）上传到网页，后台逐页改写成一看就懂、容易记住的中文或英文，
**原来的排版、图片、表格线都不动**，最后拼回一本完整的 PDF 供下载。

在你自己的电脑上用 Docker 运行，浏览器打开 <http://localhost:8000> 使用。

### 第一次使用

1. 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/) 并打开它。
2. 下载这个仓库：
   ```bash
   git clone https://github.com/chunhua1780/translator.git
   cd translator
   ```
3. 复制配置文件，然后用记事本打开 `.env`，在 `ANTHROPIC_API_KEY=` 后面填上你的 Claude API 密钥
   （在 <https://console.anthropic.com/> 申请）：
   ```bash
   cp .env.example .env
   ```
4. 启动：
   ```bash
   docker compose up -d --build
   ```
5. 浏览器打开 <http://localhost:8000>，选择 PDF 和输出语言，点“上传并开始翻译”。

关掉页面或电脑都没关系：每翻好一页就保存一页，重新启动后会从停下的地方接着做，已经翻好的页不会重复收费。

停止：`docker compose down`。翻译结果保存在 `data/` 文件夹里。

### 先用测试模式看排版

把 `.env` 里的 `REWRITER=claude` 改成 `REWRITER=mock`，再运行 `docker compose up -d`。
测试模式不调用 Claude、不花钱，只在每段文字前面加上“【测试】”，用来检查排版效果。

### 统一术语

在 `data/` 文件夹里新建 `glossary.txt`，每行写一个术语的固定译法，例如：

```
Flight Crew Operating Manual = 飞行机组操作手册
Parking brake = 停留刹车
Beacon light = 防撞灯
```

全书改写时都会按这份表用词。改了术语表之后要重启一次（`docker compose restart`）。

### 费用

默认使用 Claude Opus 5.5。网页上会实时显示每个任务已经花了多少钱。可以先选几页
（“从第几页 / 到第几页”）试一下，看看效果和单页费用，再翻整本。

### 工作原理

1. 用 PyMuPDF 逐页读出每段文字的位置、字号、颜色和粗细。
2. 每页的所有段落一起发给 Claude，按编号逐段改写成“说人话”的版本。数字、单位、件号、
   章节编号和警告事项都要求原样保留。
3. 抹掉原文（图片和线条保留），把新文字写回原来的方框；放不下时自动缩小字号。
4. 扫描版页面没有文字层，先用 Tesseract 识别文字，再用周围的底色盖住原文后写入新文字。
5. 每 25 页拼一次，最后合成整本 PDF，书签也会保留。

### 目前的限制

- 改写后的字体统一为无衬线体，不会沿用原来的字体。
- 竖排或旋转的文字保持原样，不改写。
- 改写比原文长很多时，字会变小。
- 改写版只适合学习和记忆，不能代替官方手册用于实际操作。
- 手册内容会发送到 Anthropic 的 API 进行处理，使用前请确认公司允许。

### 可选设置（`.env`）

| 设置 | 默认值 | 说明 |
|---|---|---|
| `CLAUDE_MODEL` | `claude-opus-5-5` | 使用的模型 |
| `CLAUDE_EFFORT` | `medium` | 思考强度：`low` / `medium` / `high` |
| `CONCURRENCY` | `4` | 同时翻译几页 |
| `OCR_LANGUAGES` | `eng+chi_sim` | 扫描页识别的语言 |
