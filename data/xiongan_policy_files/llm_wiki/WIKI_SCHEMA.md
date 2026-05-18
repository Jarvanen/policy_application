# LLM Wiki Schema

这个目录由脚本维护，用于把雄安政策文件编译成可持续问答的 Markdown wiki。

## 目录约定

- `policies/`：每条政策一个页面，事实来自 `policy_md/`、`declaration_guides/`、`policy_rules.jsonl`。
- `topics/`：政策关键词和资质主题页，例如高新技术企业、专精特新、知识产权。
- `industries/`：产业方向页，例如软件/人工智能/数字技术、生命科学/医药医疗。
- `applicant_types/`：申报主体页，例如企业、个人/人才、高校/科研院所。
- `agencies/`：发文单位页。
- `qa/`：值得沉淀的问答结果。
- `index.md`：内容索引，问答前优先读取。
- `log.md`：构建和问答日志。
- `gaps.md`：缺失字段和需要人工补充的信息。
- `contradictions.md`：冲突、过期和口径差异记录。

## 维护规则

1. 原始来源只读，不修改 `policy_md/`、`declaration_guides/`、`policy_rules.jsonl`。
2. 回答问题时先查 `index.md`，再读相关 wiki 页面；必要时回查原始政策 Markdown。
3. 答案必须给出来源页面或原始文件路径。
4. 如果问题产生了可复用结论，可以写入 `qa/` 并在 `log.md` 追加记录。
5. 不确定的信息要明确标注为“需核实”，不要编造政策条件。
