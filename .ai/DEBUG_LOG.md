# Debug Log

> 记录 Bug 的症状、根因、修复方案和预防措施。按时间倒序。

---

## Bug #1: WSL bash -c 引号嵌套导致命令执行乱码

**Date:** 2026-08-10
**Agent:** Claude Code

**Symptom:**
从 Windows PowerShell 通过 `wsl -d Ubuntu-26.04 -- bash -c "..."` 执行复杂 bash 命令时，嵌套引号导致输出乱码或命令中断。

**Root Cause:**
- PowerShell 和 bash 两层引号解析冲突
- WSL 输出编码问题（UTF-8 vs Windows 代码页）

**Solution:**
- 使用脚本文件 + stdin 管道方式：先 `Write` 脚本到 Windows 路径，再 `wsl -- bash -c "tr -d '\r' < /mnt/c/.../script.sh | bash"`
- 避免在 `bash -c` 中使用嵌套双引号

**Prevention:**
- 复杂命令统一走脚本文件
- CI/CD 和部署脚本不受影响（直接在 Linux 环境执行）

---

## Bug #2: npm global bin 不在非交互 shell PATH 中

**Date:** 2026-08-10

**Symptom:**
`wsl -d Ubuntu-26.04 -- bash -c "claude --version"` 报 command not found，但交互式 shell 中可用。

**Root Cause:**
- nvm 安装的 Node.js 将 bin 放在 `~/.nvm/versions/node/v24.19.0/bin`
- `.bashrc` 中的 nvm 初始化在非交互 shell 中不执行
- npm global install 的二进制（claude, opencode）在同一目录

**Solution:**
- 符号链接到 `/usr/local/bin`（node, npm, npx, uv, uvx）— 已在 setup 脚本中完成
- npm global bin 中的工具（claude, opencode）使用完整路径调用

**Prevention:**
- 未来的全局工具也添加 symlink 到 `/usr/local/bin`
- 或者在脚本中显式设置 PATH

---

## Bug #3: pytest baseline 测试集：backend/app/ 目录不存在报错

**Date:** 2026-08-10

**Symptom:**
`ls backend/app/` 在某些路径下报 No such file or directory。

**Root Cause:**
- 工作目录不在仓库根目录时，相对路径解析错误
- 某些工具在子目录执行时找不到 `backend/app/`

**Solution:**
始终确保 `cd` 到仓库根目录后再执行命令。

**Prevention:**
- 所有脚本和 Makefile target 开头加 `cd "$(git rev-parse --show-toplevel)"`

---

## Bug #4: shellcheck 缺失导致 2 个 release-control 测试失败

**Date:** 2026-08-10

**Symptom:**
`test_v120_release_control.py::test_control_installer_passes_bare_shellcheck_x` 和 `test_edge_release_control.py::test_issue153_runbooks_are_clean_shell_copyable_and_define_inputs` 失败。

**Root Cause:**
WSL 开发环境未安装 shellcheck。

**Solution:**
```bash
sudo apt-get install -y shellcheck
```

**Prevention:**
- `scripts/bootstrap-dev.sh` 应检查 shellcheck 是否安装
- CI 环境已包含（GitHub Actions ubuntu-latest 内置 shellcheck）

---

## Template

```markdown
## Bug #N: [标题]

**Date:** YYYY-MM-DD
**Agent:** [Claude Code / Human]

**Symptom:**
[用户看到什么]

**Root Cause:**
[根本原因]

**Solution:**
[修复了什么]

**Prevention:**
[如何防止复发]
```
