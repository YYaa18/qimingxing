extends Window

signal prefs_applied(avatar_scale: float, roam_on: bool, earcons_on: bool, ambient_on: bool, auto_hide: bool)
signal scale_previewed(avatar_scale: float)
signal reset_position_requested
signal reload_mcp_requested
signal open_web_requested

var http_base: String = "http://127.0.0.1:18790"

var _http: HTTPRequest
var _pending_mode: String = "" # "get" | "post"
var _loaded_settings: Dictionary = {}
var _profile_names: Array[String] = []
var _is_syncing_ui: bool = false

# UI widgets — Tab 1: 外观与交互
var _scale_slider: HSlider
var _scale_val_label: Label
var _roam_check: CheckButton
var _earcons_check: CheckButton
var _ambient_check: CheckButton
var _auto_hide_check: CheckButton

# UI widgets — Tab 2: 模型与 API Key
var _default_profile_opt: OptionButton
var _ds_key_edit: LineEdit
var _ds_key_status: Label
var _prof_select_opt: OptionButton
var _prof_model_edit: LineEdit
var _prof_url_edit: LineEdit
var _prof_key_edit: LineEdit
var _prof_key_status: Label

# UI widgets — Tab 3: 语音与隐私
var _audio_enabled_check: CheckButton
var _tts_voice_opt: OptionButton
var _ptt_hotkey_opt: OptionButton
var _privacy_mode_opt: OptionButton
var _inbox_dir_edit: LineEdit

# Footer
var _status_label: Label
var _save_btn: Button

const VOICE_OPTIONS := ["stella", "tide"]
const HOTKEY_OPTIONS := ["right_option", "left_option", "option+space", "ctrl+space", "fn"]
const PRIVACY_OPTIONS := ["off", "meta_only", "full_on_demand"]


func _ready() -> void:
	_http = HTTPRequest.new()
	add_child(_http)
	_http.request_completed.connect(_on_http_completed)
	close_requested.connect(func() -> void: visible = false)
	_build_ui()


func open_settings(
	cur_http_base: String,
	cur_scale: float,
	cur_roam: bool,
	cur_earcons: bool,
	cur_ambient: bool
) -> void:
	http_base = cur_http_base.trim_suffix("/")
	_is_syncing_ui = true
	_scale_slider.value = cur_scale * 100.0
	_scale_val_label.text = "%d%%" % int(round(cur_scale * 100.0))
	_roam_check.button_pressed = cur_roam
	_earcons_check.button_pressed = cur_earcons
	_ambient_check.button_pressed = cur_ambient
	_ds_key_edit.text = ""
	_prof_key_edit.text = ""
	_is_syncing_ui = false

	visible = true
	grab_focus()
	_fetch_backend_settings()


func _make_bg_panel() -> PanelContainer:
	var pc := PanelContainer.new()
	pc.set_anchors_preset(Control.PRESET_FULL_RECT)
	var sb := StyleBoxFlat.new()
	sb.bg_color = Color(0.08, 0.11, 0.16, 1.0)
	sb.content_margin_left = 14
	sb.content_margin_right = 14
	sb.content_margin_top = 12
	sb.content_margin_bottom = 12
	pc.add_theme_stylebox_override("panel", sb)
	return pc


func _add_section_label(parent: Control, txt: String) -> Label:
	var lbl := Label.new()
	lbl.text = txt
	lbl.add_theme_font_size_override("font_size", 13)
	lbl.add_theme_color_override("font_color", Color(0.65, 0.88, 1.0))
	parent.add_child(lbl)
	return lbl


func _add_hint_label(parent: Control, txt: String) -> Label:
	var lbl := Label.new()
	lbl.text = txt
	lbl.autowrap_mode = TextServer.AUTOWRAP_WORD_SMART
	lbl.add_theme_font_size_override("font_size", 11)
	lbl.add_theme_color_override("font_color", Color(0.66, 0.72, 0.80))
	parent.add_child(lbl)
	return lbl


func _build_ui() -> void:
	var bg := _make_bg_panel()
	add_child(bg)

	var root_col := VBoxContainer.new()
	root_col.add_theme_constant_override("separation", 10)
	bg.add_child(root_col)

	var tabs := TabContainer.new()
	tabs.size_flags_vertical = Control.SIZE_EXPAND_FILL
	root_col.add_child(tabs)

	# --- Tab 1: 外观与交互 ---
	var tab1_scroll := ScrollContainer.new()
	tab1_scroll.name = "外观与交互"
	tabs.add_child(tab1_scroll)

	var t1 := VBoxContainer.new()
	t1.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t1.add_theme_constant_override("separation", 10)
	tab1_scroll.add_child(t1)

	_add_section_label(t1, "桌面真身大小与动态行为")
	var scale_row := HBoxContainer.new()
	scale_row.add_theme_constant_override("separation", 8)
	t1.add_child(scale_row)
	var scale_lbl := Label.new()
	scale_lbl.text = "角色缩放:"
	scale_row.add_child(scale_lbl)
	_scale_slider = HSlider.new()
	_scale_slider.min_value = 50.0
	_scale_slider.max_value = 130.0
	_scale_slider.step = 2.0
	_scale_slider.value = 78.0
	_scale_slider.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_scale_slider.value_changed.connect(func(v: float) -> void:
		_scale_val_label.text = "%d%%" % int(round(v))
		if not _is_syncing_ui:
			scale_previewed.emit(v / 100.0)
	)
	scale_row.add_child(_scale_slider)
	_scale_val_label = Label.new()
	_scale_val_label.custom_minimum_size = Vector2(46, 0)
	_scale_val_label.text = "78%"
	scale_row.add_child(_scale_val_label)

	_roam_check = CheckButton.new()
	_roam_check.text = "允许闲置时在桌面自由漫步"
	_roam_check.button_pressed = true
	t1.add_child(_roam_check)

	_earcons_check = CheckButton.new()
	_earcons_check.text = "启用工具与提醒微音效 (Earcons)"
	_earcons_check.button_pressed = true
	t1.add_child(_earcons_check)

	_ambient_check = CheckButton.new()
	_ambient_check.text = "启用柔和环境氛围音（播报自动压低）"
	t1.add_child(_ambient_check)

	_auto_hide_check = CheckButton.new()
	_auto_hide_check.text = "截屏看屏前自动短暂隐身防自拍"
	_auto_hide_check.button_pressed = true
	t1.add_child(_auto_hide_check)

	_add_hint_label(t1, "提示：将启明星拖到屏幕左/右/上边缘松手即可像加速器一样贴边隐藏，鼠标靠近边缘自动滑出。")

	var pos_btn := Button.new()
	pos_btn.text = "重置位置 / 取消贴边"
	pos_btn.pressed.connect(func() -> void: reset_position_requested.emit())
	t1.add_child(pos_btn)

	# --- Tab 2: 模型与 API Key ---
	var tab2_scroll := ScrollContainer.new()
	tab2_scroll.name = "模型与 API Key"
	tabs.add_child(tab2_scroll)

	var t2 := VBoxContainer.new()
	t2.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t2.add_theme_constant_override("separation", 8)
	tab2_scroll.add_child(t2)

	_add_section_label(t2, "默认对话 Profile")
	_default_profile_opt = OptionButton.new()
	t2.add_child(_default_profile_opt)

	_add_section_label(t2, "DeepSeek 统一 API Key（一键同步至对话 / OCR / 联网搜索 / 语音）")
	var ds_row := HBoxContainer.new()
	ds_row.add_theme_constant_override("separation", 6)
	t2.add_child(ds_row)
	_ds_key_edit = LineEdit.new()
	_ds_key_edit.secret = true
	_ds_key_edit.placeholder_text = "留空则保留当前已保存的密钥"
	_ds_key_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	ds_row.add_child(_ds_key_edit)
	var ds_eye := Button.new()
	ds_eye.text = "显示"
	ds_eye.pressed.connect(func() -> void:
		_ds_key_edit.secret = not _ds_key_edit.secret
		ds_eye.text = "隐藏" if not _ds_key_edit.secret else "显示"
	)
	ds_row.add_child(ds_eye)
	_ds_key_status = _add_hint_label(t2, "当前状态: 加载中…")

	var sep := HSeparator.new()
	t2.add_child(sep)

	_add_section_label(t2, "单个 Profile 高级覆盖（可选）")
	_prof_select_opt = OptionButton.new()
	_prof_select_opt.item_selected.connect(_on_profile_selected)
	t2.add_child(_prof_select_opt)

	_prof_model_edit = LineEdit.new()
	_prof_model_edit.placeholder_text = "Model ID（例如 deepseek-chat / gpt-4o）"
	t2.add_child(_prof_model_edit)

	_prof_url_edit = LineEdit.new()
	_prof_url_edit.placeholder_text = "Base URL（例如 https://api.deepseek.com/v1）"
	t2.add_child(_prof_url_edit)

	var pkey_row := HBoxContainer.new()
	pkey_row.add_theme_constant_override("separation", 6)
	t2.add_child(pkey_row)
	_prof_key_edit = LineEdit.new()
	_prof_key_edit.secret = true
	_prof_key_edit.placeholder_text = "独立 API Key 覆盖（留空保留原值）"
	_prof_key_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	pkey_row.add_child(_prof_key_edit)
	var p_eye := Button.new()
	p_eye.text = "显示"
	p_eye.pressed.connect(func() -> void:
		_prof_key_edit.secret = not _prof_key_edit.secret
		p_eye.text = "隐藏" if not _prof_key_edit.secret else "显示"
	)
	pkey_row.add_child(p_eye)
	_prof_key_status = _add_hint_label(t2, "")

	# --- Tab 3: 语音与隐私 ---
	var tab3_scroll := ScrollContainer.new()
	tab3_scroll.name = "语音与隐私"
	tabs.add_child(tab3_scroll)

	var t3 := VBoxContainer.new()
	t3.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	t3.add_theme_constant_override("separation", 8)
	tab3_scroll.add_child(t3)

	_add_section_label(t3, "语音交互 (DeepSeek ASR & TTS)")
	_audio_enabled_check = CheckButton.new()
	_audio_enabled_check.text = "启用流式语音识别与语音播报"
	_audio_enabled_check.button_pressed = true
	t3.add_child(_audio_enabled_check)

	var voice_row := HBoxContainer.new()
	t3.add_child(voice_row)
	var vl := Label.new()
	vl.text = "TTS 播报音色: "
	voice_row.add_child(vl)
	_tts_voice_opt = OptionButton.new()
	_tts_voice_opt.add_item("stella（自然女声）", 0)
	_tts_voice_opt.add_item("tide（沉稳男声）", 1)
	_tts_voice_opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	voice_row.add_child(_tts_voice_opt)

	var ptt_row := HBoxContainer.new()
	t3.add_child(ptt_row)
	var hl := Label.new()
	hl.text = "按住说话热键: "
	ptt_row.add_child(hl)
	_ptt_hotkey_opt = OptionButton.new()
	for idx in range(HOTKEY_OPTIONS.size()):
		_ptt_hotkey_opt.add_item(HOTKEY_OPTIONS[idx], idx)
	_ptt_hotkey_opt.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	ptt_row.add_child(_ptt_hotkey_opt)
	_add_hint_label(t3, "说明：修改全局按住说话热键后，下次重启 Senses 生效。")

	_add_section_label(t3, "隐私模式与收件箱路径")
	_privacy_mode_opt = OptionButton.new()
	_privacy_mode_opt.add_item("off（完全关闭桌面感知）", 0)
	_privacy_mode_opt.add_item("meta_only（仅窗口标题，推荐）", 1)
	_privacy_mode_opt.add_item("full_on_demand（按需截屏看屏）", 2)
	t3.add_child(_privacy_mode_opt)

	var inbox_row := HBoxContainer.new()
	t3.add_child(inbox_row)
	var il := Label.new()
	il.text = "收件箱目录: "
	inbox_row.add_child(il)
	_inbox_dir_edit = LineEdit.new()
	_inbox_dir_edit.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	inbox_row.add_child(_inbox_dir_edit)

	var ops_row := HBoxContainer.new()
	ops_row.add_theme_constant_override("separation", 8)
	t3.add_child(ops_row)
	var mcp_btn := Button.new()
	mcp_btn.text = "热重载 MCP 工具"
	mcp_btn.pressed.connect(func() -> void: reload_mcp_requested.emit())
	ops_row.add_child(mcp_btn)
	var web_btn := Button.new()
	web_btn.text = "打开 Web 调试台"
	web_btn.pressed.connect(func() -> void: open_web_requested.emit())
	ops_row.add_child(web_btn)

	# Footer bar
	var footer := HBoxContainer.new()
	footer.add_theme_constant_override("separation", 8)
	root_col.add_child(footer)

	_status_label = Label.new()
	_status_label.text = "就绪"
	_status_label.size_flags_horizontal = Control.SIZE_EXPAND_FILL
	_status_label.add_theme_font_size_override("font_size", 11)
	_status_label.add_theme_color_override("font_color", Color(0.68, 0.84, 0.96))
	footer.add_child(_status_label)

	var close_btn := Button.new()
	close_btn.text = "关闭"
	close_btn.pressed.connect(func() -> void: visible = false)
	footer.add_child(close_btn)

	_save_btn = Button.new()
	_save_btn.text = "保存并生效"
	_save_btn.pressed.connect(_on_save_pressed)
	footer.add_child(_save_btn)


func _fetch_backend_settings() -> void:
	_pending_mode = "get"
	_status_label.text = "正在同步设置…"
	var err := _http.request("%s/api/config/settings" % http_base, [], HTTPClient.METHOD_GET)
	if err != OK:
		_status_label.text = "读取配置失败 (%d)" % err


func _on_http_completed(_result: int, code: int, _headers: PackedStringArray, body: PackedByteArray) -> void:
	var txt := body.get_string_from_utf8()
	var parsed: Variant = JSON.parse_string(txt)
	if code < 200 or code >= 300 or typeof(parsed) != TYPE_DICTIONARY:
		_status_label.text = "请求异常 HTTP %d" % code
		_save_btn.disabled = false
		return

	var dict: Dictionary = parsed
	if _pending_mode == "get":
		_apply_loaded_settings(dict)
		_status_label.text = "已读取脱敏配置（密钥以明文安全隐藏）"
	elif _pending_mode == "post":
		_save_btn.disabled = false
		_ds_key_edit.text = ""
		_prof_key_edit.text = ""
		var s_dict: Dictionary = dict.get("settings", {})
		if not s_dict.is_empty():
			_apply_loaded_settings(s_dict)
		_status_label.text = "保存成功，已写入 config.yaml (0600) 并热更新生效！"


func _apply_loaded_settings(data: Dictionary) -> void:
	_loaded_settings = data
	var ds_info: Dictionary = data.get("shared_deepseek_key", {})
	if bool(ds_info.get("has_key", false)):
		_ds_key_status.text = "当前 DeepSeek 密钥: %s（留空则保持不变）" % str(ds_info.get("masked", "已配置"))
	else:
		_ds_key_status.text = "当前状态: 尚未配置 DeepSeek API Key"

	var profiles: Dictionary = data.get("profiles", {})
	_profile_names.clear()
	_default_profile_opt.clear()
	_prof_select_opt.clear()

	var def_prof := str(data.get("default_profile", ""))
	var def_idx := 0
	var idx := 0
	for pname in profiles.keys():
		var p: Dictionary = profiles[pname]
		var lbl := str(p.get("label", pname))
		_profile_names.append(str(pname))
		_default_profile_opt.add_item("%s (%s)" % [pname, lbl], idx)
		_prof_select_opt.add_item(str(pname), idx)
		if str(pname) == def_prof:
			def_idx = idx
		idx += 1
	if not _profile_names.is_empty():
		_default_profile_opt.select(def_idx)
		_prof_select_opt.select(def_idx)
		_on_profile_selected(def_idx)

	var audio_sec: Dictionary = data.get("audio", {})
	_audio_enabled_check.button_pressed = bool(audio_sec.get("enabled", true))
	var tts_sec: Dictionary = audio_sec.get("tts", {})
	var v := str(tts_sec.get("voice", "stella"))
	var v_idx := VOICE_OPTIONS.find(v)
	_tts_voice_opt.select(v_idx if v_idx >= 0 else 0)

	var ptt_sec: Dictionary = audio_sec.get("ptt", {})
	var hk := str(ptt_sec.get("hotkey", "right_option"))
	var h_idx := HOTKEY_OPTIONS.find(hk)
	_ptt_hotkey_opt.select(h_idx if h_idx >= 0 else 0)

	var priv_sec: Dictionary = data.get("privacy", {})
	var pmode := str(priv_sec.get("default_mode", "meta_only"))
	var pm_idx := PRIVACY_OPTIONS.find(pmode)
	_privacy_mode_opt.select(pm_idx if pm_idx >= 0 else 1)

	var batch_sec: Dictionary = data.get("batch_context", {})
	_inbox_dir_edit.text = str(batch_sec.get("inbox_dir", "~/Desktop/给启明星"))

	var godot_sec: Dictionary = data.get("godot", {})
	_auto_hide_check.button_pressed = bool(godot_sec.get("auto_hide_on_capture", true))


func _on_profile_selected(idx: int) -> void:
	if idx < 0 or idx >= _profile_names.size():
		return
	var pname := _profile_names[idx]
	var profiles: Dictionary = _loaded_settings.get("profiles", {})
	var p: Dictionary = profiles.get(pname, {})
	_prof_model_edit.text = str(p.get("model", ""))
	_prof_url_edit.text = str(p.get("base_url", ""))
	_prof_key_edit.text = ""
	if bool(p.get("has_key", false)):
		_prof_key_status.text = "该 Profile 已配置密钥: %s" % str(p.get("key_masked", "****"))
	else:
		_prof_key_status.text = "该 Profile 暂无独立密钥"


func _on_save_pressed() -> void:
	var new_scale := _scale_slider.value / 100.0
	var roam_on := _roam_check.button_pressed
	var earcons_on := _earcons_check.button_pressed
	var ambient_on := _ambient_check.button_pressed
	var auto_hide := _auto_hide_check.button_pressed
	prefs_applied.emit(new_scale, roam_on, earcons_on, ambient_on, auto_hide)

	var payload: Dictionary = {
		"audio": {
			"enabled": _audio_enabled_check.button_pressed,
			"tts": {"voice": VOICE_OPTIONS[clampi(_tts_voice_opt.selected, 0, VOICE_OPTIONS.size() - 1)]},
			"ptt": {"hotkey": HOTKEY_OPTIONS[clampi(_ptt_hotkey_opt.selected, 0, HOTKEY_OPTIONS.size() - 1)]},
		},
		"privacy": {
			"default_mode": PRIVACY_OPTIONS[clampi(_privacy_mode_opt.selected, 0, PRIVACY_OPTIONS.size() - 1)],
		},
		"batch_context": {
			"inbox_dir": _inbox_dir_edit.text.strip_edges(),
		},
		"godot": {
			"auto_hide_on_capture": auto_hide,
			"earcons_enabled": earcons_on,
		},
	}

	if _default_profile_opt.selected >= 0 and _default_profile_opt.selected < _profile_names.size():
		payload["default_profile"] = _profile_names[_default_profile_opt.selected]

	var ds_key := _ds_key_edit.text.strip_edges()
	if ds_key != "":
		payload["shared_deepseek_api_key"] = ds_key

	if _prof_select_opt.selected >= 0 and _prof_select_opt.selected < _profile_names.size():
		var sel_name := _profile_names[_prof_select_opt.selected]
		var prof_patch: Dictionary = {
			"model": _prof_model_edit.text.strip_edges(),
			"base_url": _prof_url_edit.text.strip_edges(),
		}
		var pk := _prof_key_edit.text.strip_edges()
		if pk != "":
			prof_patch["api_key"] = pk
		payload["profiles"] = {sel_name: prof_patch}

	_pending_mode = "post"
	_save_btn.disabled = true
	_status_label.text = "正在保存并应用设置…"
	var err := _http.request(
		"%s/api/config/settings" % http_base,
		["Content-Type: application/json"],
		HTTPClient.METHOD_POST,
		JSON.stringify(payload)
	)
	if err != OK:
		_save_btn.disabled = false
		_status_label.text = "保存请求失败 (%d)" % err
