#!/usr/bin/env bash
# 审计生产 workspace/tools 与 git 的差异 —— 投放前必跑, 也是投放后的判据。
#
# 用法(在目标机上跑, 或本地 ssh 进去跑):
#     audit-workspace-drift.sh <git-ref> [workspace...] [subtree] [filter]
#     audit-workspace-drift.sh origin/main                              # tools, 全部三份
#     audit-workspace-drift.sh origin/main workspace                    # tools, 只 gateway 那份
#     audit-workspace-drift.sh origin/main workspace config '*.yaml'    # 换成审 config/
#
# 为什么要有这个脚本, 而不是让文档写一串 md5 命令:
#
# `workspace/tools/` 那 241 个业务文件**不在镜像里**, 是 bind mount 到机器上的,
# 靠人手 cp 投放。2026-09-14 实测的后果: 生产 `_feishu_spec.py` 是新版而
# `_feishu_api_impl.py` 是旧版 —— 9-12 那次投放投了前者漏了后者, 于是 195 条飞书
# API 护栏规则一条都不生效, 唯一线索是一行 INFO 日志。漏投不报错、不变红、
# 功能表面跑通, 这种失败没有判据可言。
#
# 2026-09-18 同一类事故的第二例, 换了个目录: `workspace/config/` 不在投放清单里,
# 于是 `config/meeting-sop.yaml` 从来没投上去过。生产的 gateway 是
# `--default-agent /workspace`, 所以 `AGENT_ROOT` 就是 `/workspace`、
# `MEETING_SOP_CONFIG_PATH` 解析成 `/workspace/config/meeting-sop.yaml`, 缺了它
# 周中对齐会那条 pipeline 每次运行都在 `meeting_pipeline_run.py` 里抛
# `RuntimeError: 会议 SOP 配置缺失` 并整场失败 —— 模型、转写、收件人都正常,
# 唯独判定口径读不到。所以**别只审 tools**: `config/` 与 `skills/` 是同一类
# 人手投放物, 换个 subtree 参数各审一遍。
#
# 本脚本要回答的正是"差哪些文件", 且必须区分四类 —— 混成一个数字就再也分不开了:
#
#   同     md5 相等(LF 归一化后)。
#   落后   与 git 不同, 但 mtime 是**整秒** → 部署投放留下的旧版, 可安全覆盖。
#   领先   与 git 不同, 且 mtime **带纳秒** → 有人在生产上就地写入, 覆盖即丢代码。
#   缺失   git 有而生产没有。
#
# mtime 纳秒位这个判据来自 2026-09-12 的取证: 部署投放(tar/cp -p 保留源 mtime, 或
# CI 产物)落在整秒, 就地编辑落在带纳秒的时刻。uid 不是判据(投放和手改都可能是 root)。
#
# ⚠️ "领先"必须人工定归属, 不能自动覆盖。已知一例: `_card_dsl.py` 是未合并的
# PR #867 (fork Twin-Ghosts) 的代码再往前改的, 生产是该 PR 的超集。把它当"旧版"
# 覆盖会静默丢掉 607 行功能(原生 table 渲染 / bind-field 回写 / action_id 撞车防护)。
#
# ⚠️ 两台私有 workspace 各缺 73/71 个文件, 它们不是 gateway 的副本而是 8-07 的旧
# 快照。**不要**对它们做"补依赖闭包"式的增量投放: 补一条链会连带换新
# `_feishu_impl.py`, 而旧 `_feishu/bitable.py` 立刻让 40+ 文件断链, 工具数从
# 198 掉到 87(2026-09-12 实测并已回滚)。闭包的边界是整棵依赖树, 不是看得见的报错。
set -euo pipefail

REF="${1:-origin/main}"
shift || true
WORKSPACES=("$@")
# 尾部可选参数: [subtree] [filter]。从后往前摘 —— 先摘 filter(它一定在最后),
# 再摘 subtree。
#
# subtree 的判据是**值对得上白名单**, 不是「长得像路径」。位置判据(含斜杠)
# 会把 `audit ... config` 这种写法判成 workspace, 于是静默去审
# `$PROD_ROOT/config/tools`、报出一整片假「缺失」—— 审计脚本给出错误结论比
# 报错更糟。白名单只有两个名字, 维护成本近乎零; 加新 subtree 时同时加在这里。
_AUDIT_SUBTREES="tools config"
# subtree 名 -> 仓库内路径。两者不同源: 仓库里是 `agents/feishu/<name>`,
# 生产侧是 `<workspace>/<name>`。
_subtree_path() { printf 'agents/feishu/%s' "$1"; }
#: 逐字比较, 不用 `case` 的子串匹配。子串匹配会把任一**包含** subtree 名的 workspace
#: 吃掉 —— 实测 workspace 名 `ok` 命中 `tools`, 于是 `audit ... ok` 被判成
#: `subtree=ok`; 而 WORKSPACES 变空又会回落成默认三份, 症状就成了「只审一份却报三份的
#: 差异」, 与传入的名字毫无关系。代价: `tools` / `config` 这两个名字留作 subtree 专用。
_is_subtree_name() {
  local name
  for name in $_AUDIT_SUBTREES; do
    [ "$name" = "$1" ] && return 0
  done
  return 1
}
# filter 的判据是「含 `*` 或 `?`」, 与位置无关, 所以它不会与 workspace 名混淆。
if [ ${#WORKSPACES[@]} -gt 0 ]; then
  last="${WORKSPACES[${#WORKSPACES[@]}-1]}"
  if [[ "$last" == *'*'* || "$last" == *'?'* ]]; then
    FILTER="$last"; unset 'WORKSPACES[${#WORKSPACES[@]}-1]'
  fi
fi
if [ ${#WORKSPACES[@]} -gt 0 ]; then
  last="${WORKSPACES[${#WORKSPACES[@]}-1]}"
  if _is_subtree_name "$last"; then
    SUBTREE="$(_subtree_path "$last")"; unset 'WORKSPACES[${#WORKSPACES[@]}-1]'
  fi
fi

# unset 掉尾部元素后下标会留洞, 重新压实 —— 否则 `${#arr[@]}` 对, 但遍历会带上空洞。
WORKSPACES=(${WORKSPACES[@]+"${WORKSPACES[@]}"})
[ ${#WORKSPACES[@]} -eq 0 ] && WORKSPACES=(workspace workspace-luolin workspace-chengxx)

PROD_ROOT="${PROD_ROOT:-/srv/haitun/psi-agent}"
# 默认仍是 tools —— 既有调用方(文档 / 跑道)一字不用改。审 config/ 时显式传
# `config` 加对应 filter。tools 只比 .py 是 2026-09-14 定下的: 该目录当时实测
# 241 个业务文件全是 .py, 而 `find -name '*.py'` 正是为了防止顶层 glob 漏掉
# `_feishu/` 私有子目录。config/ 里是 .yaml, 换个 filter 即可 —— 差异只有这一项。
SUBTREE="${SUBTREE:-$(_subtree_path tools)}"
FILTER="${FILTER:-*.py}"
# 生产侧目录名 = subtree 的末段。历史上写死成 `tools`, 于是审 config 时会去比
# `$PROD_ROOT/<workspace>/tools/*.yaml` —— 那个目录下一条都匹配不到, 报出来的是
# 一整片假「缺失」。默认值仍是 tools, 所以既有行为逐字节不变。
SUBDIR="${SUBTREE##*/}"
REPO="${REPO:-}"

# 仓库位置: 显式给 REPO, 否则找一个能用的 clone。不 clone 新的 —— 目标机的 GitHub
# 是间歇故障(实测 TLS recv error -110), 静默拉半份比拉不到更糟。
if [ -z "$REPO" ]; then
  # 挑历史最全的那个 clone, 不是第一个撞上的。落后/领先的判据是"blob 在不在仓库里",
  # 历史缺失会把"落后"误判成"领先"。实测目标机上 /tmp/rel-482d970c 只有 729 个
  # commit 而 /tmp/rel-5565f4bd 有 3133 个 —— 按 glob 顺序取正好取到残缺那份。
  best=0
  for c in /tmp/rel-check /tmp/rel-* .; do
    [ -d "$c/.git" ] || continue
    n=$(git -C "$c" rev-list --count --all 2>/dev/null) || continue
    if [ "${n:-0}" -gt "$best" ]; then best="$n"; REPO="$c"; fi
  done
fi
[ -n "$REPO" ] && [ -d "$REPO/.git" ] || { echo "找不到 git clone。用 REPO=<path> 指定。" >&2; exit 2; }

cd "$REPO"
SHA=$(git rev-parse --short "$REF" 2>/dev/null) || { echo "解析不出 ref: $REF" >&2; exit 2; }
echo "基准: $REF = $SHA  ($(git log -1 --format=%ad --date=short "$SHA"))"
echo "仓库: $REPO"
echo "范围: $SUBTREE  ($FILTER)"
echo

# git 侧快照。用 archive 而非 checkout: 不动工作树, 也不受别的会话影响。
SNAP=$(mktemp -d)
trap 'rm -rf "$SNAP"' EXIT
git archive "$SHA" "$SUBTREE" | tar -x -C "$SNAP"
GITDIR="$SNAP/$SUBTREE"
[ -d "$GITDIR" ] || { echo "ref 里没有 $SUBTREE" >&2; exit 2; }

# 全树走, 不是顶层 glob。2026-09-14 实测: 顶层 glob 漏掉 6 个 `_feishu/` 私有子目录
# 文件, 其中 3 个的缺失直接导致工具加载失败。
( cd "$GITDIR" && find . -name "$FILTER" | sed 's|^\./||' ) | LC_ALL=C sort -u > "$SNAP/git_names"
echo "git 侧: $(wc -l < "$SNAP/git_names") 个 $FILTER"
echo

RC=0
for W in "${WORKSPACES[@]}"; do
  T="$PROD_ROOT/$W/$SUBDIR"
  [ -d "$T" ] || { echo "跳过 $W: $T 不存在"; echo; continue; }

  same=0; stale=0; ahead=0; missing=0
  : > "$SNAP/stale_$W"; : > "$SNAP/ahead_$W"; : > "$SNAP/missing_$W"

  while read -r f; do
    p="$T/$f"
    if [ ! -f "$p" ]; then
      missing=$((missing+1)); echo "$f" >> "$SNAP/missing_$W"; continue
    fi
    # LF 归一化: 生产是 LF、仓库检出可能是 CRLF, 裸比对会报几乎全不一致。
    a=$(tr -d '\r' < "$GITDIR/$f" | md5sum | cut -d' ' -f1)
    b=$(tr -d '\r' < "$p"         | md5sum | cut -d' ' -f1)
    if [ "$a" = "$b" ]; then
      same=$((same+1)); continue
    fi
    # 落后 vs 领先: 判据是"这份内容在仓库里存在过吗", 不是 mtime。
    #
    # 2026-09-14 实测: 只看 mtime 纳秒位会误判。`meeting_pipeline_run.py` 带纳秒
    # (14:19 之外的一次手工投放), 按 mtime 判成"领先"、脚本拒绝覆盖; 但它的内容与
    # commit 880d9831 逐字节相同, 其实是**落后** 5 天。根因: 不带 `-p` 的 `cp` 会把
    # mtime 设成"此刻", 而此刻天然带纳秒 —— 手工投放与就地编辑在 mtime 上无法区分。
    #
    # `git hash-object` 算出的是 blob SHA; 只要仓库里存在这个 blob, 说明这份内容
    # 是某个 commit 里的版本, 覆盖它不会丢任何没进 git 的代码。反之才是真"领先"。
    # 前提: 仓库 blob 与生产文件都是 LF(已核 .gitattributes 与 blob 内容), 所以
    # 直接对生产文件算 hash 是同口径比对。
    blob=$(git hash-object "$p")
    if git cat-file -e "$blob" 2>/dev/null; then
      stale=$((stale+1)); echo "$f" >> "$SNAP/stale_$W"
    else
      ahead=$((ahead+1)); printf '%s  (%s)\n' "$f" "$(stat -c %y "$p" | cut -c1-19)" >> "$SNAP/ahead_$W"
    fi
  done < "$SNAP/git_names"

  ( cd "$T" && find . -name "$FILTER" | sed 's|^\./||' ) | LC_ALL=C sort -u > "$SNAP/prod_$W"
  # comm 要求两边都排过序, 且必须同一 collation —— LC_ALL=C 两处都加。不加会报
  # "not in sorted order" 并吐出自相矛盾的结果(同一文件同时出现在两侧), 实测踩过。
  comm -13 "$SNAP/git_names" "$SNAP/prod_$W" > "$SNAP/only_$W" || true
  only=$(wc -l < "$SNAP/only_$W")

  printf '=== %s\n' "$W"
  printf '    同=%-4s 落后=%-4s 领先=%-4s 缺失=%-4s 生产独有=%s\n' "$same" "$stale" "$ahead" "$missing" "$only"

  if [ "$ahead" -gt 0 ]; then
    echo "    ⚠ 领先(就地写入, 覆盖即丢代码 —— 必须人工定归属):"
    sed 's/^/      /' "$SNAP/ahead_$W"
    RC=1
  fi
  [ "$stale" -gt 0 ] && { echo "    落后(可安全覆盖):"; sed 's/^/      /' "$SNAP/stale_$W"; }
  [ "$missing" -gt 0 ] && { echo "    缺失:"; sed 's/^/      /' "$SNAP/missing_$W"; }
  [ "$only" -gt 0 ] && { echo "    生产独有(git 已删或从未有过):"; sed 's/^/      /' "$SNAP/only_$W"; }
  echo
done

# 退出码 1 = 有"领先"文件待定归属。CI/脚本据此停下, 而不是继续覆盖。
exit $RC
