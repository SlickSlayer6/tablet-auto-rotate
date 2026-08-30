-- Add after the wildcard monitor rule in ~/.config/hypr/monitors.lua
-- Establishes the normal laptop-mode baseline. The daemon changes only the
-- runtime transform.
hl.monitor({
  output = "eDP-1",
  mode = "preferred",
  position = "auto",
  scale = 1,
  transform = 0,
})
