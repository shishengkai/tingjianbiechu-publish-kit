# 文件工具

`tingjianbiechu_publish_kit/file_kit.py`（CLI：`tjbc-publish inspect|deliver|verify`）只处理本地文件，不调用模型、不发布抖音。需要 Python 3.12+、PATH 中的 `ffprobe`；图片尺寸转换还需要 `ffmpeg`。

## 支持的项目形态

1. **magicdub-cli（v0.1 默认）**：任务目录含 `state.json`，成片与字幕通常为 `exports/final.mp4`、`exports/final.srt`。样例根目录：`~/Movies/MagicDub/cli/`。
2. **legacy MagicDub skill 项目**：含 `project.json` 与 `exports/*.mp4`（仍支持）。

下列 `<…>` 均为需要替换的路径，路径含空格时保留引号。

## 1. 只读定位

```sh
tjbc-publish inspect --project "<magicdub-cli 任务目录或 legacy 项目目录>"
# 可选：--source-dir "<原视频下载目录>"
```

`--project` 也可指向该项目的 `project.json` 或紧邻项目根目录的 `exports/`。`--source-dir` 可省略，元数据缺失不阻断。脚本不会在整个电脑上搜索或以修改时间猜项目。

定位顺序：显式 `--video` / `--subtitle` → `project.json` 的 `latest_export` / `latest_subtitle` → 唯一 MP4 / 同名 SRT / 唯一 SRT。失效指针或多候选会报错，核对后显式指定正确文件；不能静默换用其他项目。视频、中文字幕必须位于这个 MagicDub 项目内。其他覆盖参数：

- `--original-text`：原文 SRT 或 transcript JSON 的绝对路径；未给时先找项目根目录的 `transcript.json`，再看原素材目录唯一 SRT。实际读内容后判断它是否为原文。
- `--preview`：明确选择的参考图片绝对路径；否则读取下载目录 `manifest.json` 中的 `preview_file`。

JSON 结果提供最终视频、字幕、原文候选、原始 `title` / `text`、预览图、视频显示尺寸、输出目录与建议版本。缺字段的 warnings 是核对提示，不是要求停止。来源视频哈希优先与 MagicDub 的 `source_sha256` 比较，否则尝试视频 ID；明确不一致报错，证据不足交由 agent 核对。manifest 中的相对文件路径不得逃出原素材目录。

最终 MP4 的父目录就是成品输出目录。旋转和非方形像素会折算为实际显示宽高；无法解释的旋转报错。`completed_with_quality_flags` 只作为现有译制状态提示，不阻断素材制作，也不代表发布已验收。

## 2. 准备文案和生成记录

实际读稿、完成生成并查看图片后，准备 UTF-8 JSON。示意如下；必须替换示意内容，不得把占位文字当最终文案：

```json
{
  "title": "最终推荐标题",
  "description": "一段与视频内容相符的描述。",
  "hashtags": ["具体主题", "访谈", "听见别处"],
  "cover_title": "实际放入封面的标题",
  "prompts": {
    "first_frame": "首帧图实际使用的完整提示词",
    "portrait": "竖版列表图实际使用的完整提示词",
    "landscape": "横版列表图实际使用的完整提示词"
  },
  "reviewed_covers": ["first_frame", "portrait", "landscape"],
  "editorial_notes": "选题角度、来源核对和必要限制",
  "research_sources": [],
  "visual_review_notes": "逐张实际检查所得的结果"
}
```

`title`、`description`、非空 `hashtags` 数组必填。脚本自动把唯一固定落款补到描述末尾并规范化话题的 `#`。所有额外字段原样留在记录中，不写入发布 TXT；不要保存密钥、账户信息或无关私人资料。

完整生成提示词、参考图片路径、生成工具／模型名称和实际修改记录应保存在 JSON 的附加字段里，方便后续复用。若经过多次调整，`prompts` 保留最终有效提示词，额外 `generation_history` 数组保留实际调用和修正过程。不记录模型内部思考过程。

只有真实查看且合格的角色才能放入 `reviewed_covers`。脚本只核对声明存在，不会替 agent 看图。没有完成生图时，只交付文案，不能伪造这些字段。

## 3. 保存四个成品

```sh
python3 "<技能目录>/scripts/publish_kit.py" deliver \
  --project "<具体 MagicDub 项目目录>" \
  --source-dir "<原视频下载目录>" \
  --content "<内容 JSON>" \
  --first-frame "<生成工具返回的首帧图片>" \
  --portrait "<生成工具返回的竖版图片>" \
  --landscape "<生成工具返回的横版图片>"
```

接受 PNG、JPEG、WebP 输入，输出 PNG。比例误差不超过 1% 时做尺寸重采样至要求的像素数；超过 1% 拒绝导出，回到图像模型重新构图。不会自动裁切、扩图、改字或拉伸明显错误的比例。尺寸合格的真实 PNG 原样复制。文件工具处理的是导出规格，不替代 imagegen 的创作与修改。

版本默认自动递增，也可用正整数 `--version N` 指定。存在同版本任一成品或记录时拒绝覆盖。执行前完成所有图片预检；写入途中失败会保留已经写出的文件及失败记录，下次使用新版本，不删除旧结果冒充成功。

## 4. 局部修改

只更新文案时：

```sh
python3 "<技能目录>/scripts/publish_kit.py" deliver \
  --project "<具体 MagicDub 项目目录>" \
  --content "<修改后的内容 JSON>" --only copy
```

只重做横版时使用 `--only landscape --landscape "<新图片>"`。仍需完整文案 JSON，用来保存新图片采用的内容背景；只写被选中的成品。部分更新标记 `partial_update`，不会伪装成四个文件都已更新。同一版本不能追加，下一次始终用新版本；其余角色继续使用之前的文件，交付时说明对应版本。如果修改标题，检查三张现有封面是否仍表达同一主题，必要时同步生成受影响图片。

## 5. 复核与记录

```sh
python3 "<技能目录>/scripts/publish_kit.py" verify --record "<deliver 返回的 record 路径>"
```

记录在 `<项目>/provenance/tingjianbiechu-publish-kit/<素材名>/vN/record.json`，包含技能版本、输入路径及 SHA-256、元数据和定位证据、文案、完整提示词、检查声明、成品路径／尺寸／哈希。日常使用的 TXT/PNG 在视频同目录，记录不混入待复制文案。

`verify` 检查记录范围、文件存在性、内容哈希、封面目标尺寸及 TXT 落款。只验证本次记录的成品，不要求原始输入永久保持不变；输入哈希用于追溯。返回 `file_checks_passed` 不代表美术效果、字幕事实、流量或抖音发布已核验。命令成功退出 0，错误退出 2 并在 stderr 返回 JSON；不要忽略报错后继续宣称成功。
