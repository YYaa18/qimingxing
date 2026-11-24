#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="交给启明星"
SERVICES_DIR="${QIMINGXING_SERVICES_DIR:-${HOME}/Library/Services}"
WORKFLOW_DIR="${SERVICES_DIR}/${SERVICE_NAME}.workflow"
CONTENTS_DIR="${WORKFLOW_DIR}/Contents"
ENDPOINT="${QIMINGXING_ATTACH_URL:-http://127.0.0.1:18790/api/context/attach_paths}"

mkdir -p "${CONTENTS_DIR}/QuickLook"

cat > "${CONTENTS_DIR}/Info.plist" <<'PLIST_EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>NSServices</key>
	<array>
		<dict>
			<key>NSMenuItem</key>
			<dict>
				<key>default</key>
				<string>交给启明星</string>
			</dict>
			<key>NSMessage</key>
			<string>runWorkflowAsService</string>
			<key>NSRequiredContext</key>
			<dict>
				<key>NSApplicationIdentifier</key>
				<string>com.apple.finder</string>
			</dict>
			<key>NSSendFileTypes</key>
			<array>
				<string>public.item</string>
			</array>
		</dict>
	</array>
</dict>
</plist>
PLIST_EOF

UUID_WORKFLOW="$(uuidgen 2>/dev/null || echo '11111111-2222-3333-4444-555555555555')"
UUID_INPUT="$(uuidgen 2>/dev/null || echo '66666666-7777-8888-9999-aaaaaaaaaaaa')"
UUID_OUTPUT="$(uuidgen 2>/dev/null || echo 'bbbbbbbb-cccc-dddd-eeee-ffffffffffff')"

SHELL_SCRIPT_BODY=$(cat <<SCRIPT_EOF
import json, os, subprocess, sys, urllib.request
paths = [os.path.abspath(p) for p in sys.argv[1:] if p.strip()]
if not paths:
    sys.exit(0)
payload = json.dumps({"paths": paths, "source": "quick_action", "append": True}).encode("utf-8")
req = urllib.request.Request("${ENDPOINT}", data=payload, headers={"Content-Type": "application/json"}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=4) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        msg = f"已挂载 {data.get('accepted', len(paths))} 项路径到启明星"
except Exception as exc:
    msg = f"挂载失败，请确认启明星已启动: {exc}"
subprocess.run(["osascript", "-e", f'display notification {json.dumps(msg)} with title "启明星（Qimingxing）"'], check=False)
SCRIPT_EOF
)

cat > "${CONTENTS_DIR}/document.wflow" <<WFLOW_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>AMApplicationBuild</key>
	<string>523</string>
	<key>AMApplicationVersion</key>
	<string>2.10</string>
	<key>AMDocumentVersion</key>
	<string>2</string>
	<key>actions</key>
	<array>
		<dict>
			<key>action</key>
			<dict>
				<key>AMAccepts</key>
				<dict>
					<key>Container</key>
					<string>List</string>
					<key>Optional</key>
					<true/>
					<key>Types</key>
					<array>
						<string>com.apple.cocoa.path</string>
					</array>
				</dict>
				<key>AMActionVersion</key>
				<string>2.0.3</string>
				<key>AMApplication</key>
				<array>
					<string>Automator</string>
				</array>
				<key>AMParameterProperties</key>
				<dict>
					<key>COMMAND_STRING</key>
					<dict/>
					<key>CheckedForUserDefaultShell</key>
					<dict/>
					<key>inputMethod</key>
					<dict/>
					<key>shell</key>
					<dict/>
					<key>source</key>
					<dict/>
				</dict>
				<key>AMProvides</key>
				<dict>
					<key>Container</key>
					<string>List</string>
					<key>Types</key>
					<array>
						<string>com.apple.cocoa.path</string>
					</array>
				</dict>
				<key>ActionBundlePath</key>
				<string>/System/Library/Automator/Run Shell Script.action</string>
				<key>ActionName</key>
				<string>Run Shell Script</string>
				<key>ActionParameters</key>
				<dict>
					<key>COMMAND_STRING</key>
					<string>python3 - "\$@" &lt;&lt;'PYEOF'
${SHELL_SCRIPT_BODY}
PYEOF</string>
					<key>CheckedForUserDefaultShell</key>
					<true/>
					<key>inputMethod</key>
					<integer>1</integer>
					<key>shell</key>
					<string>/bin/zsh</string>
					<key>source</key>
					<string></string>
				</dict>
				<key>BundleIdentifier</key>
				<string>com.apple.RunShellScript</string>
				<key>CFBundleVersion</key>
				<string>2.0.3</string>
				<key>CanShowSelectedItemsWhenRun</key>
				<false/>
				<key>CanShowWhenRun</key>
				<true/>
				<key>Category</key>
				<array>
					<string>AMCategoryUtilities</string>
				</array>
				<key>Class Name</key>
				<string>RunShellScriptAction</string>
				<key>InputUUID</key>
				<string>${UUID_INPUT}</string>
				<key>Keywords</key>
				<array>
					<string>Shell</string>
					<string>Script</string>
				</array>
				<key>OutputUUID</key>
				<string>${UUID_OUTPUT}</string>
				<key>UUID</key>
				<string>${UUID_WORKFLOW}</string>
				<key>UnlocalizedApplications</key>
				<array>
					<string>Automator</string>
				</array>
			</dict>
			<key>isViewVisible</key>
			<integer>1</integer>
		</dict>
	</array>
	<key>connectors</key>
	<dict/>
	<key>workflowMetaData</key>
	<dict>
		<key>applicationBundleID</key>
		<string>com.apple.finder</string>
		<key>applicationBundleIDsByPath</key>
		<dict>
			<key>/System/Library/CoreServices/Finder.app</key>
			<string>com.apple.finder</string>
		</dict>
		<key>applicationPath</key>
		<string>/System/Library/CoreServices/Finder.app</string>
		<key>applicationPaths</key>
		<array>
			<string>/System/Library/CoreServices/Finder.app</string>
		</array>
		<key>inputTypeIdentifier</key>
		<string>com.apple.Automator.fileSystemObject</string>
		<key>outputTypeIdentifier</key>
		<string>com.apple.Automator.nothing</string>
		<key>presentationMode</key>
		<integer>15</integer>
		<key>processesInput</key>
		<false/>
		<key>serviceApplicationBundleID</key>
		<string>com.apple.finder</string>
		<key>serviceApplicationPath</key>
		<string>/System/Library/CoreServices/Finder.app</string>
		<key>serviceInputTypeIdentifier</key>
		<string>com.apple.Automator.fileSystemObject</string>
		<key>serviceOutputTypeIdentifier</key>
		<string>com.apple.Automator.nothing</string>
		<key>serviceProcessesInput</key>
		<false/>
		<key>systemImageName</key>
		<string>NSActionTemplate</string>
		<key>useAutomaticInputType</key>
		<false/>
		<key>workflowTypeIdentifier</key>
		<string>com.apple.Automator.servicesMenu</string>
	</dict>
</dict>
</plist>
WFLOW_EOF

if command -v /System/Library/CoreServices/pbs >/dev/null 2>&1; then
  /System/Library/CoreServices/pbs -flush >/dev/null 2>&1 || true
fi

echo "✅ 已安装 Finder 右键快速操作: ${WORKFLOW_DIR}"
echo "   在 Finder 选中任意文件或文件夹 -> 右键「快速操作」->「${SERVICE_NAME}」即可挂载路径引用给启明星。"
