# Trello Mark Read Power-Up V5

Connector:
https://teklayty.github.io/airtable-trello-drive/trello-powerup/index.html

V5 fixes the REST client bug in V4: `t.getRestApi()` returns a Promise and
must be awaited/resolved before calling `isAuthorized()`, `authorize()`, or
`del()`.

The first click can open an authorization popup. After authorization,
close the popup and click Mark Read again. The button then removes the
`New Message` label from the current card.

This version intentionally shows Mark Read on editable cards during testing.
After successful testing, the button can be restricted to cards carrying
the New Message label.
