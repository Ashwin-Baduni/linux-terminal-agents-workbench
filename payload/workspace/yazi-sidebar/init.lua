-- Mouse-friendly yazi: double-click to open, plus "Up" and "Close" buttons in
-- the top bar. Single click still just selects.

-- Double-click: folders open inside yazi, files open with their app
-- (Markdown in glow, HTML in the browser, images in the image viewer, ...).
local DOUBLE_CLICK_SECONDS = 0.4
local last_click = { time = 0, url = "" }

local original_entity_click = Entity.click
function Entity:click(event, up)
	original_entity_click(self, event, up)
	if up or not event.is_left then
		return
	end
	local now, url = ya.time(), tostring(self._file.url)
	if url == last_click.url and now - last_click.time < DOUBLE_CLICK_SECONDS then
		last_click = { time = 0, url = "" }
		ya.emit(self._file.cha.is_dir and "enter" or "open", {})
	else
		last_click = { time = now, url = url }
	end
end

-- Top-bar buttons, drawn at the right edge in this order.
local BUTTONS = {
	{ label = " ↑ Up ", action = "leave" },
	{ label = " ⋯ Actions ", action = "workspace-actions" },
	{ label = " ✕ Close ", action = "quit" },
}
local BUTTON_STYLE = ui.Style():fg("#d0d0d0"):bg("#262626")

Header:children_add(function()
	local spans = {}
	for _, button in ipairs(BUTTONS) do
		spans[#spans + 1] = ui.Span(" ")
		spans[#spans + 1] = ui.Span(button.label):style(BUTTON_STYLE)
	end
	return ui.Line(spans)
end, 9000, Header.RIGHT)

function Header:click(event, up)
	if up or not event.is_left then
		return
	end
	-- Walk the buttons from the right edge leftwards.
	local right = self._area.x + self._area.w
	for i = #BUTTONS, 1, -1 do
		local width = ui.Line(BUTTONS[i].label):width()
		if event.x >= right - width and event.x < right then
			if BUTTONS[i].action == "workspace-actions" then
                ya.emit("plugin", { "workspace" })
            else
                ya.emit(BUTTONS[i].action, {})
            end
			return
		end
		right = right - width - 1
	end
end

require("git"):setup {}
