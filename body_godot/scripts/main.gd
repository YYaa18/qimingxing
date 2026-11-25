extends Control

const SETTINGS_SCENE := preload("res://scenes/SettingsWindow.tscn")
const QUICK_ENTRY_SCENE := preload("res://scenes/QuickEntryWindow.tscn")
const PREFS_PATH := "user://qimingxing_body_prefs.cfg"

@onready var ws: Node = $WSClient
@onready var audio: Node = $AudioPlayer
@onready var avatar: Control = $AvatarController
@onready var overlay: Control = $UIOverlay
@onready var tray: Node = $TrayManager

var _http: HTTPRequest
var _pending_http_label: String = ""
var _settings_win: Window
var _quick_entry_win: Window
var _cached_active_profile: String = ""
var _cached_profiles: Array = []
var _shortcuts: Dictionary = {
	"ptt_hotkey": "right_option",
	"chat_input_hotkey": "option+space",
	"cancel_hotkey": "option+esc",
	"toggle_dock_hotkey": "option+h",
	"settings_hotkey": "option+k",
}
var _last_hotkey_trigger_ms: Dictionary = {}


func _ready() -> void:
	_http = HTTPRequest.new()
	add_child(_http)
	_http.request_completed.connect(_on_http_completed)

	_load_audio_prefs()
	tray.call("sync_toggles", avatar.call("is_roam_enabled"), audio.earcons_enabled, audio.call("is_ambient_enabled"))
	tray.call("sync_shortcut_hints", str(_shortcuts.get("chat_input_hotkey", "")), str(_shortcuts.get("ptt_hotkey", "")), str(_shortcuts.get("settings_hotkey", "")))
	tray.call("sync_status", false, _cached_active_profile)
	overlay.call("set_avatar_scale", avatar.call("get_avatar_scale"))

	ws.connected_changed.connect(func(ok: bool) -> void:
		tray.call("sync_status", ok, _cached_active_profile)
		if ok:
			overlay.show_transient_status("启明星已连接", 2.5)
		else:
			audio.stop_tts_immediately()
			overlay.show_transient_status("连接断开，正在重连…", 3.0)
	)
	ws.envelope_received.connect(_on_envelope)

	audio.amplitude_changed.connect(avatar.set_tts_amplitude)
	audio.speaking_state_changed.connect(avatar.set_tts_speaking)

	avatar.avatar_clicked.connect(func() -> void:
		audio.play_earcon("done")
		_toggle_quick_entry()
	)
	avatar.context_menu_requested.connect(func() -> void:
		tray.call("popup_at_cursor")
	)
	avatar.scale_changed.connect(func(sc: float) -> void:
		overlay.call("set_avatar_scale", sc)
		overlay.show_transient_status("当前大小: %d%%（可在托盘设置调整）" % int(round(sc * 100.0)), 1.8)
	)
	avatar.dock_state_changed.connect(func(edge: String, tucked: bool) -> void:
		overlay.set_dock_tucked(tucked)
		if edge != "" and tucked:
			overlay.show_transient_status("已贴边隐藏（悬停边缘即可呼出）", 1.8)
	)
	avatar.files_dropped_queued.connect(overlay.queue_dropped_files)

	overlay.passthrough_rects_changed.connect(avatar.set_ui_interactive_rects)
	overlay.interaction_active_changed.connect(avatar.set_interaction_active)
	overlay.approval_decided.connect(_on_approval_decided)
	overlay.clarification_answered.connect(_on_clarification_answered)
	overlay.cancel_requested.connect(_on_cancel_requested)
	overlay.drop_queue_submitted.connect(_on_drop_submitted)
	overlay.desktop_action_requested.connect(_on_desktop_action)

	tray.open_chat_input_requested.connect(_open_chat_input)
	tray.open_settings_requested.connect(func() -> void: _open_settings_window(0))
	tray.open_shortcuts_requested.connect(func() -> void: _open_settings_window(3))
	tray.open_mcp_config_requested.connect(func() -> void: _open_settings_window(4))
	tray.toggle_roam_requested.connect(func(on: bool) -> void:
		avatar.set_roam_enabled(on)
		overlay.show_transient_status("桌面自由漫步: %s" % ("已开启" if on else "已暂停"), 1.8)
	)
	tray.toggle_earcons_requested.connect(func(on: bool) -> void:
		audio.earcons_enabled = on
		_save_audio_prefs()
	)
	tray.toggle_ambient_requested.connect(func(on: bool) -> void:
		audio.set_ambient_enabled(on)
		_save_audio_prefs()
	)
	tray.reset_position_requested.connect(func() -> void:
		avatar.call("reset_to_default_position")
		overlay.show_transient_status("已回到默认桌面位置", 1.8)
	)
	tray.reload_mcp_requested.connect(_reload_mcp_tools)
	tray.open_web_requested.connect(func() -> void:
		OS.shell_open("%s/" % ws.http_base)
	)
	tray.quit_requested.connect(func() -> void:
		get_tree().quit()
	)

	call_deferred("_ensure_settings_window")
	call_deferred("_ensure_quick_entry_window")


func _load_audio_prefs() -> void:
	var cfg := ConfigFile.new()
	if cfg.load(PREFS_PATH) == OK:
		audio.earcons_enabled = bool(cfg.get_value("audio_prefs", "earcons_enabled", true))
		var amb := bool(cfg.get_value("audio_prefs", "ambient_enabled", false))
		if amb:
			audio.set_ambient_enabled(true)


func _save_audio_prefs() -> void:
	var cfg := ConfigFile.new()
	cfg.load(PREFS_PATH)
	cfg.set_value("audio_prefs", "earcons_enabled", audio.earcons_enabled)
	cfg.set_value("audio_prefs", "ambient_enabled", audio.call("is_ambient_enabled"))
	cfg.save(PREFS_PATH)


func _ensure_settings_window() -> Window:
	if _settings_win == null:
		_settings_win = SETTINGS_SCENE.instantiate() as Window
		get_tree().root.add_child(_settings_win)
		_settings_win.prefs_applied.connect(_on_settings_prefs_applied)
		_settings_win.scale_previewed.connect(func(sc: float) -> void:
			avatar.set_avatar_scale(sc)
			overlay.call("set_avatar_scale", sc)
		)
		_settings_win.reset_position_requested.connect(func() -> void:
			avatar.call("reset_to_default_position")
		)
		_settings_win.reload_mcp_requested.connect(_reload_mcp_tools)
		_settings_win.open_web_requested.connect(func() -> void:
			OS.shell_open("%s/" % ws.http_base)
		)
	return _settings_win


func _open_settings_window(initial_tab: int = 0) -> void:
	var win := _ensure_settings_window()
	win.call(
		"open_settings",
		ws.http_base,
		avatar.call("get_avatar_scale"),
		avatar.call("is_roam_enabled"),
		audio.earcons_enabled,
		audio.call("is_ambient_enabled"),
		initial_tab
	)


func _on_settings_prefs_applied(
	avatar_scale: float,
	roam_on: bool,
	earcons_on: bool,
	ambient_on: bool,
	_auto_hide: bool
) -> void:
	avatar.set_avatar_scale(avatar_scale)
	overlay.call("set_avatar_scale", avatar_scale)
	avatar.set_roam_enabled(roam_on)
	audio.earcons_enabled = earcons_on
	audio.set_ambient_enabled(ambient_on)
	_save_audio_prefs()
	tray.call("sync_toggles", roam_on, earcons_on, ambient_on)


func _reload_mcp_tools() -> void:
	_post_json("%s/api/mcp/reload" % ws.http_base, {}, "已热重载 MCP 工具列表")


func _on_envelope(event_type: String, turn_id: String, payload: Dictionary) -> void:
	match event_type:
		"system.welcome":
			var prof := str(payload.get("active_profile", ""))
			var profs: Variant = payload.get("profiles", [])
			if typeof(profs) == TYPE_ARRAY:
				_cached_profiles = profs
			var prof_disp := prof
			for p_var in _cached_profiles:
				if typeof(p_var) == TYPE_DICTIONARY and str((p_var as Dictionary).get("name", "")) == prof:
					var lbl_s := str((p_var as Dictionary).get("label", "")).strip_edges()
					if lbl_s != "":
						prof_disp = lbl_s
					break
			if prof != "":
				_cached_active_profile = prof
				overlay.show_transient_status("启明星已上线 (%s)" % prof_disp, 2.2)
			tray.call("sync_status", bool(ws.call("is_connected_open")), prof_disp)
			if _quick_entry_win != null:
				_quick_entry_win.call("sync_profiles", _cached_active_profile, _cached_profiles)
			var welcome_sc: Variant = payload.get("shortcuts", {})
			if typeof(welcome_sc) == TYPE_DICTIONARY:
				_apply_shortcuts_payload(welcome_sc)
		"system.shortcuts_sync":
			_apply_shortcuts_payload(payload)
		"system.hotkey_triggered":
			_handle_shortcut_action(str(payload.get("action", "")))
		"system.capture_prepare":
			var hide_ms := int(payload.get("hide_ms", 150))
			avatar.set_capture_hidden(true, maxf(1.5, float(hide_ms) / 1000.0 + 1.5))
		"system.capture_done":
			avatar.set_capture_hidden(false)
		"avatar.state":
			avatar.set_avatar_state(str(payload.get("emotion", "neutral")), str(payload.get("activity", "idle")))
		"output.turn_start":
			overlay.on_turn_start(turn_id)
		"output.text_delta":
			overlay.on_text_delta(str(payload.get("delta", "")), bool(payload.get("is_final", false)))
		"output.turn_end":
			overlay.on_turn_end()
			if str(payload.get("finish_reason", "")) == "cancelled":
				audio.stop_tts_immediately()
			elif str(payload.get("finish_reason", "")) == "completed":
				audio.play_earcon("done")
		"output.tool_status":
			var status := str(payload.get("status", ""))
			var label := str(payload.get("label", ""))
			if status == "running":
				audio.play_earcon("tool")
			if label != "":
				overlay.show_transient_status(label, 3.8)
		"output.proactive_notice":
			audio.play_earcon("notice")
			var title := str(payload.get("title", "提醒"))
			var content := str(payload.get("content", ""))
			overlay.show_transient_status("%s: %s" % [title, content], 8.0)
		"output.artifact_capsule":
			overlay.show_artifact_capsule(
				str(payload.get("title", "产物")),
				str(payload.get("path", "")),
				str(payload.get("media_type", "file")),
				payload.get("actions", []),
			)
		"tool.approval_request":
			var tname := str(payload.get("tool_name", ""))
			var det: Variant = payload.get("details", {})
			var det_dict: Dictionary = det if typeof(det) == TYPE_DICTIONARY else {}
			var kind := str(det_dict.get("kind", ""))
			if tname == "ask_user" or kind == "clarification":
				audio.play_earcon("notice")
				avatar.set_avatar_state("thinking", "thinking")
				var opts: Variant = det_dict.get("options", [])
				overlay.call(
					"show_clarification_request",
					str(payload.get("approval_id", "")),
					str(payload.get("turn_id", turn_id)),
					str(det_dict.get("question", payload.get("summary", "请补充确认"))),
					opts if typeof(opts) == TYPE_ARRAY else [],
				)
			else:
				audio.play_earcon("approval")
				avatar.set_approval_alert(true)
				overlay.show_approval_request(
					str(payload.get("approval_id", "")),
					str(payload.get("turn_id", turn_id)),
					str(payload.get("summary", "需要审批")),
				)
		"audio.output_start":
			audio.start_tts_stream(int(payload.get("sample_rate", 24000)))
		"audio.output_chunk":
			audio.push_pcm_chunk(str(payload.get("data_b64", "")))
		"audio.output_end":
			audio.finish_tts_stream(str(payload.get("finish_reason", "completed")))
		"system.error":
			var msg := str(payload.get("message", ""))
			if msg != "":
				overlay.show_transient_status(msg, 6.0)


func _on_approval_decided(approval_id: String, turn_id: String, approved: bool) -> void:
	ws.send_event("tool.approval_response", {
		"approval_id": approval_id,
		"turn_id": turn_id,
		"approved": approved,
		"remember_for_session": false,
	}, turn_id)


func _on_clarification_answered(approval_id: String, turn_id: String, approved: bool, answer_text: String) -> void:
	ws.send_event("tool.approval_response", {
		"approval_id": approval_id,
		"turn_id": turn_id,
		"approved": approved,
		"remember_for_session": false,
		"answer_text": answer_text,
	}, turn_id)


func _on_cancel_requested(turn_id: String) -> void:
	audio.stop_tts_immediately()
	ws.send_event("input.cancel", {"reason": "godot_emergency_stop"}, turn_id)


func _apply_shortcuts_payload(payload: Dictionary) -> void:
	for k in ["ptt_hotkey", "chat_input_hotkey", "cancel_hotkey", "toggle_dock_hotkey", "settings_hotkey"]:
		if payload.has(k):
			_shortcuts[k] = str(payload.get(k, "")).strip_edges().to_lower()
	tray.call(
		"sync_shortcut_hints",
		str(_shortcuts.get("chat_input_hotkey", "")),
		str(_shortcuts.get("ptt_hotkey", "")),
		str(_shortcuts.get("settings_hotkey", ""))
	)


func _ensure_quick_entry_window() -> Window:
	if _quick_entry_win == null:
		_quick_entry_win = QUICK_ENTRY_SCENE.instantiate() as Window
		get_tree().root.add_child(_quick_entry_win)
		_quick_entry_win.prompt_submitted.connect(_on_quick_entry_submitted)
		_quick_entry_win.profile_selected.connect(_on_quick_entry_profile_selected)
		_quick_entry_win.new_chat_requested.connect(_on_quick_entry_new_chat)
		_quick_entry_win.visibility_changed_custom.connect(func(vis: bool) -> void:
			avatar.set_interaction_active(vis)
		)
		if not _cached_profiles.is_empty():
			_quick_entry_win.call("sync_profiles", _cached_active_profile, _cached_profiles)
	return _quick_entry_win


func _open_chat_input() -> void:
	var qe := _ensure_quick_entry_window()
	qe.call("open_quick_entry")


func _toggle_quick_entry() -> void:
	var qe := _ensure_quick_entry_window()
	qe.call("toggle_quick_entry")


func _handle_shortcut_action(action: String) -> void:
	if action == "":
		return
	var now_ticks := Time.get_ticks_msec()
	var prev_ticks := int(_last_hotkey_trigger_ms.get(action, -10000))
	if now_ticks - prev_ticks < 280:
		return
	_last_hotkey_trigger_ms[action] = now_ticks

	match action:
		"chat_input":
			_toggle_quick_entry()
		"cancel":
			audio.stop_tts_immediately()
			ws.send_event("input.cancel", {"reason": "godot_hotkey_cancel"})
			overlay.show_transient_status("已通过快捷键急停当前播报/任务", 1.8)
		"toggle_dock":
			var hidden := bool(avatar.call("toggle_edge_hide"))
			overlay.show_transient_status("已一键贴边隐藏" if hidden else "已恢复默认位置", 1.8)
		"settings":
			_open_settings_window(3)


func _input(event: InputEvent) -> void:
	if not (event is InputEventKey):
		return
	var ev := event as InputEventKey
	if not ev.pressed or ev.echo:
		return
	if ev.meta_pressed and not ev.alt_pressed and not ev.ctrl_pressed and ev.keycode == KEY_COMMA:
		_open_settings_window(0)
		get_viewport().set_input_as_handled()
		return
	var mapping := [
		["chat_input", str(_shortcuts.get("chat_input_hotkey", ""))],
		["cancel", str(_shortcuts.get("cancel_hotkey", ""))],
		["toggle_dock", str(_shortcuts.get("toggle_dock_hotkey", ""))],
		["settings", str(_shortcuts.get("settings_hotkey", ""))],
	]
	for entry in mapping:
		if _match_hotkey_event(ev, str(entry[1])):
			_handle_shortcut_action(str(entry[0]))
			get_viewport().set_input_as_handled()
			return


func _match_hotkey_event(ev: InputEventKey, spec_str: String) -> bool:
	var norm := spec_str.strip_edges().to_lower()
	if norm == "" or norm in ["disabled", "off", "none"]:
		return false
	var parts := norm.split("+", false)
	if parts.is_empty():
		return false
	var key_name := parts[parts.size() - 1].strip_edges()
	var target_key := _key_name_to_keycode(key_name)
	if target_key == KEY_NONE:
		return false

	var want_alt := false
	var want_ctrl := false
	var want_cmd := false
	var want_shift := false
	for i in range(parts.size() - 1):
		var m := parts[i].strip_edges()
		if m in ["option", "alt", "left_option", "right_option", "left_alt", "right_alt"]:
			want_alt = true
		elif m in ["ctrl", "control", "left_ctrl", "right_ctrl"]:
			want_ctrl = true
		elif m in ["cmd", "command", "left_cmd", "right_cmd"]:
			want_cmd = true
		elif m == "shift":
			want_shift = true
		else:
			return false

	if ev.alt_pressed != want_alt or ev.ctrl_pressed != want_ctrl or ev.meta_pressed != want_cmd or ev.shift_pressed != want_shift:
		return false
	var is_fn_key := target_key >= KEY_F1 and target_key <= KEY_F12
	if not (want_alt or want_ctrl or want_cmd or is_fn_key):
		return false
	return ev.keycode == target_key or ev.physical_keycode == target_key


func _key_name_to_keycode(key_name: String) -> int:
	match key_name:
		"space":
			return KEY_SPACE
		"esc", "escape":
			return KEY_ESCAPE
		"enter", "return":
			return KEY_ENTER
		"tab":
			return KEY_TAB
		"f1":
			return KEY_F1
		"f2":
			return KEY_F2
		"f3":
			return KEY_F3
		"f4":
			return KEY_F4
		"f5":
			return KEY_F5
		"f6":
			return KEY_F6
		"f7":
			return KEY_F7
		"f8":
			return KEY_F8
		"f9":
			return KEY_F9
		"f10":
			return KEY_F10
		"f11":
			return KEY_F11
		"f12":
			return KEY_F12
	if key_name.length() == 1:
		var code := key_name.to_upper().unicode_at(0)
		if (code >= 65 and code <= 90) or (code >= 48 and code <= 57):
			return code
	return KEY_NONE


func _on_quick_entry_submitted(text: String, with_screen: bool, attached_paths: Array[String]) -> void:
	if not bool(ws.call("is_connected_open")):
		overlay.show_transient_status("尚未连接到启明星大脑，正在重连…", 3.0)
		return
	if not attached_paths.is_empty():
		_on_drop_submitted(attached_paths)
		await _http.request_completed
	if not bool(ws.call("is_connected_open")):
		return
	audio.stop_tts_immediately()
	var turn_id := "turn_godot_%d" % Time.get_ticks_msec()
	avatar.set_avatar_state("thinking", "thinking")
	if with_screen:
		ws.send_event("input.action", {"action": "ask_with_screen", "data": {"text": text}}, turn_id)
	else:
		var final_text := text if text != "" else "请分析并总结我挂载的文件内容。"
		ws.send_event("input.text", {"text": final_text}, turn_id)


func _on_quick_entry_profile_selected(profile_name: String) -> void:
	if profile_name == "" or not bool(ws.call("is_connected_open")):
		return
	ws.send_event("system.config_set", {"profile": profile_name})


func _on_quick_entry_new_chat() -> void:
	if not bool(ws.call("is_connected_open")):
		return
	ws.send_event("input.action", {"action": "clear_history"})
	overlay.show_transient_status("已开启新对话", 1.8)


func _on_drop_submitted(paths: Array[String]) -> void:
	_post_json("%s/api/context/attach_paths" % ws.http_base, {
		"paths": paths,
		"source": "godot_drop",
		"append": true,
	}, "已将 %d 项路径挂载给启明星" % paths.size())


func _on_desktop_action(action: String, path: String, _media_type: String) -> void:
	_post_json("%s/api/desktop/open_action" % ws.http_base, {
		"action": action,
		"path": path,
	}, "已执行本地动作: %s" % action)


func _post_json(url: String, body: Dictionary, ok_label: String) -> void:
	_pending_http_label = ok_label
	var err := _http.request(url, ["Content-Type: application/json"], HTTPClient.METHOD_POST, JSON.stringify(body))
	if err != OK:
		overlay.show_transient_status("本地接口请求失败 (%d)" % err, 4.0)


func _on_http_completed(_result: int, response_code: int, _headers: PackedStringArray, _body: PackedByteArray) -> void:
	if response_code >= 200 and response_code < 300:
		overlay.show_transient_status(_pending_http_label, 4.0)
	else:
		overlay.show_transient_status("操作返回异常 HTTP %d" % response_code, 4.0)
