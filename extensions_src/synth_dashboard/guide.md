# Control Deck (Dashboard)

Every adjustable value of this SyntH as a control: knobs for numbers, toggles for
booleans, selects, chip editors, colour pickers and JSON boxes, grouped per component,
with live readouts, plugin run/switch commands and an agent prompt runner.

## Use it

1. Switch the extension on and grant it **web.static** and **web.origin** (Plugins > Store).
2. Open **More… > Dashboard** in the WebUI's top bar.

## Notes

- The page calls the WebUI's `/api` as you, which is why it asks for **web.origin**
  (the Core's origin). Revoke it and the page loads but cannot read or change anything.
- It takes its colours from the WebUI theme.
- It used to be the Dashboard tab of the WebUI itself; the settings it edits are the same.
