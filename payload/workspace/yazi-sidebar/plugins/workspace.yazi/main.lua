local context = ya.sync(function()
    local hovered = cx.active.current.hovered
    return tostring(cx.active.current.cwd), hovered and tostring(hovered.url) or ""
end)

return {
    entry = function()
        local folder, selected = context()
        local pane = os.getenv("WORKSPACE_PANE") or os.getenv("TMUX_PANE") or ""
        if pane == "" then return end
        local output, err = Command(os.getenv("HOME") .. "/.local/bin/workspace-control")
            :arg({ "sidebar", folder, selected, pane, os.getenv("WORKSPACE_YAZI_ID") or "" })
            :output()
        if not output or not output.status.success then
            ya.notify({ title = "Workspace actions", content = tostring(err or output.stderr), timeout = 5, level = "error" })
        end
    end,
}
