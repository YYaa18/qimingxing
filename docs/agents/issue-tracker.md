# Issue 跟踪：GitHub

本仓库的 issue 和 spec 都存放在 GitHub issues 中（`YYaa18/qimingxing`）。所有操作使用 `gh` CLI。

## 约定

- **创建 issue**：`gh issue create --title "..." --body "..."`。多行正文用 heredoc。
- **读取 issue**：`gh issue view <number> --json number,title,body,labels,comments`。
- **列出 issue**：`gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'`，配合适当的 `--label` 和 `--state` 过滤。
- **把 issue 设为某个父 issue 的子 issue**：`gh issue create --parent <parent> ...`，或事后 `gh issue edit <parent> --add-sub-issue <child>`（`gh` 2.94+）。旧版 `gh`：`gh api --method POST repos/<owner>/<repo>/issues/<parent>/sub_issues -F sub_issue_id=<child-db-id>`（数据库 id，见下方 **Blocking**）。子 issue 功能不可用时，在子 issue 正文顶部写 `Part of #<parent>`。
- **Blocking**：使用 GitHub 原生 issue 依赖关系，这是规范的、UI 可见的表示。添加依赖边：`gh issue create --blocked-by <n>,<n> ...` 或 `gh issue edit <child> --add-blocked-by <blocker>`（`gh` 2.94+）。旧版 `gh`：`gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`，其中 `<blocker-db-id>` 是阻塞方的数字**数据库 id**（`gh api repos/<owner>/<repo>/issues/<n> --jq .id`，_不是_ `#number` 也不是 `node_id`）。依赖功能不可用时，回退到在子 issue 正文顶部写一行 `Blocked by: #<n>, #<n>`。当所有阻塞方都关闭时，该 ticket 即解除阻塞。
- **统计未关闭的阻塞方数量**：`gh api repos/<owner>/<repo>/issues/<n> --jq .issue_dependencies_summary.blocked_by`（只算未关闭的；为 0 表示可以开工）。`gh issue view <n> --json blockedBy` 也会列出已关闭的阻塞方，需要过滤：`--jq '[.blockedBy.nodes[] | select(.state == "OPEN")] | length'`。
- **评论 issue**：`gh issue comment <number> --body "..."`
- **加/移除标签**：`gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **关闭**：先用 `gh issue comment <number> --body "..."` 发布说明，再 `gh issue close <number>`。对已关闭的 issue 执行 `gh issue close --comment` 会丢弃评论但仍以 0 退出。

仓库从 `git remote -v` 推断；在克隆目录内运行 `gh` 时会自动完成。

Issue 只提交到用户自己的仓库。绝不提交到 `mattpocock/skills`（这些技能的来源仓库），除非用户拥有它。在 fork 中，`gh` 可能默认指向上游父仓库，因此每次会话首次 `gh issue create` 前，用 `gh repo view --json nameWithOwner` 检查目标仓库；如果不是用户自己的仓库，先与用户确认。

## Pull requests 作为 triage 入口

**PRs as a request surface: no.** _（如果本仓库把外部 PR 当作功能请求处理，改为 `yes`；`/triage` 会读取这个开关。）_

设为 `yes` 时，PR 走与 issue 相同的标签和状态流程，使用对应的 `gh pr` 命令：

- **读取 PR**：`gh pr view <number>`，然后 `gh pr view <number> --comments`（非 TTY 环境下 `--comments` 只打印评论，不含正文），diff 用 `gh pr diff <number>`。
- **列出待 triage 的外部 PR**：`gh api --paginate 'repos/{owner}/{repo}/pulls?state=open' --jq '.[] | select(.author_association | IN("OWNER","MEMBER","COLLABORATOR") | not) | {number, title, author: .user.login, author_association, labels: [.labels[].name]}'`。
- **评论 / 打标签 / 关闭**：`gh pr comment`、`gh pr edit --add-label`/`--remove-label`、`gh pr close`。

GitHub 的 issue 和 PR 共用一套编号，裸写的 `#42` 可能是两者之一：先用 `gh pr view 42` 解析，失败再回退 `gh issue view 42`。

## 当技能要求「publish to the issue tracker」

创建一个 GitHub issue。

## 当技能要求「fetch the relevant ticket」

按上方 **读取 issue** 的方式读取。

## Wayfinding 操作

供 `/wayfinder` 使用。**map** 是一个 issue，**child** issue 作为 tickets。

- **Map**：单个 issue，标签 `wayfinder:map`，正文包含 Notes / Decisions-so-far / Fog。`gh issue create --label wayfinder:map`。
- **Child ticket**：作为 GitHub sub-issue 关联到 map 的 issue（见 **把 issue 设为某个父 issue 的子 issue**）。sub-issue 不可用时，把 child 加进 map 正文的任务列表，并在 child 正文顶部写 `Part of #<map>`。标签：`wayfinder:<type>`（`research`/`prototype`/`grilling`/`task`）。被认领后，ticket 分配给驱动它的开发者。
- **Blocking**：见约定中的 **Blocking**。
- **Frontier 查询**：列出 map 下未关闭的 children（`gh issue list --state open`，范围限定为 map 的 sub-issues / 任务列表），剔除有未关闭阻塞方（见 **统计未关闭的阻塞方数量**，或 `Blocked by` 行中的未关闭 issue）或有 assignee 的；按 map 顺序取第一个。
- **认领**：`gh issue edit <n> --add-assignee @me`，这是会话中的第一次写操作。
- **解决**：`gh issue comment <n> --body "<answer>"`，然后 `gh issue close <n>`，再把上下文指针（gist + 链接）追加到 map 的 Decisions-so-far。
