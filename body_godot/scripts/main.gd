extends Control

@onready var ws: Node = $WSClient
@onready var audio: Node = $AudioPlayer
@onready var avatar: Control = $AvatarController
@onready var overlay: Control = $UIOverlay

var _http: HTTPRequest
var _pending_http_label: String = ""


func _ready() -> void:
	_http = HTTPRequest.new()
	add_child(_http)
	_http.request_completed.connect(_on_http_completed)

	ws.connected_changed.connect(func(ok: bool) -> void:
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
		overlay.show_transient_status("我在呢～随时听候吩咐 ✨", 2.0)
	)
	avatar.scale_changed.connect(func(sc: float) -> void:
		overlay.show_transient_status("当前大小: %d%%（滚轮或 ± 随时调节）" % int(round(sc * 100.0)), 1.8)
	)
	avatar.dock_state_changed.connect(func(edge: String, tucked: bool) -> void:
		overlay.set_dock_tucked(tucked)
		if edge != "" and tucked:
			overlay.show_transient_status("已贴边隐藏（悬停边缘即可呼出）", 1.8)
	)
	avatar.files_dropped_queued.connect(overlay.queue_dropped_files)
	overlay.set_roam_button_state(avatar.is_roam_enabled())
	overlay.passthrough_rects_changed.connect(avatar.set_ui_interactive_rects)
	overlay.interaction_active_changed.connect(avatar.set_interaction_active)
	overlay.scale_step_requested.connect(avatar.adjust_avatar_scale)
	overlay.roam_toggled.connect(avatar.set_roam_enabled)
	overlay.approval_decided.connect(_on_approval_decided)
	overlay.cancel_requested.connect(_on_cancel_requested)
	overlay.drop_queue_submitted.connect(_on_drop_submitted)
	overlay.desktop_action_requested.connect(_on_desktop_action)
	overlay.earcons_toggled.connect(func(on: bool) -> void: audio.earcons_enabled = on)
	overlay.ambient_toggled.connect(audio.set_ambient_enabled)


func _on_envelope(event_type: String, turn_id: String, payload: Dictionary) -> void:
	match event_type:
		"system.welcome":
			var prof := str(payload.get("active_profile", ""))
			if prof != "":
				overlay.show_transient_status("启明星已上线 (%s)" % prof, 2.2)
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


func _on_cancel_requested(turn_id: String) -> void:
	audio.stop_tts_immediately()
	ws.send_event("input.cancel", {"reason": "godot_emergency_stop"}, turn_id)


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
