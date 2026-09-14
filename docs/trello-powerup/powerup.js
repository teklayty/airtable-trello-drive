/*
 * WhatsApp New Message - Mark Read
 * Trello Power-Up
 *
 * Replace YOUR_TRELLO_POWERUP_APP_KEY with the API Key shown by Trello
 * for this Power-Up.
 *
 * Behavior:
 *   - Shows "Mark Read" on cards that have the "New Message" label.
 *   - Clicking it removes only that label from the current card.
 *
 * No custom backend/server endpoint is required.
 */

(function () {
  "use strict";

  const POWERUP_APP_NAME = "WhatsApp New Message - Mark Read";
  const POWERUP_APP_KEY = "YOUR_TRELLO_POWERUP_APP_KEY";
  const NEW_MESSAGE_LABEL_NAME = "New Message";

  const ICON_URL =
    "data:image/svg+xml;charset=utf-8," +
    encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">' +
      '<path fill="#666" d="M12 2a10 10 0 1 0 6.32 17.75L22 22l-2.25-3.68A10 10 0 0 0 12 2Zm4.59 14.59-1.69-1.69a1 1 0 0 0-1.41 0l-.77.77a7.01 7.01 0 0 1-4.38-4.38l.77-.77a1 1 0 0 0 0-1.41L7.41 8.41A1 1 0 1 0 6 9.82l1.24 1.24a9.01 9.01 0 0 0 5.28 5.28L13.76 17A1 1 0 1 0 15.17 15.59l1.42 1.42Z"/>' +
      '</svg>'
    );

  function isNewMessageLabel(label) {
    return !!(
      label &&
      typeof label.name === "string" &&
      label.name.trim().toLowerCase() === NEW_MESSAGE_LABEL_NAME.toLowerCase()
    );
  }

  function findNewMessageLabel(card) {
    const labels = Array.isArray(card && card.labels) ? card.labels : [];
    return labels.find(isNewMessageLabel) || null;
  }

  async function markRead(t) {
    try {
      if (POWERUP_APP_KEY === "YOUR_TRELLO_POWERUP_APP_KEY") {
        await t.alert({
          message:
            "Replace YOUR_TRELLO_POWERUP_APP_KEY in powerup.js with the API Key from your Trello Power-Up."
        });
        return;
      }

      const card = await t.card("id", "labels");
      const label = findNewMessageLabel(card);

      if (!label || !label.id) {
        await t.alert({
          message: '"' + NEW_MESSAGE_LABEL_NAME + '" is already absent from this card."
        });
        return;
      }

      const rest = t.getRestApi();
      let authorized = await rest.isAuthorized();

      if (!authorized) {
        await rest.authorize({
          scope: "read,write"
        });
        authorized = await rest.isAuthorized();
      }

      if (!authorized) {
        await t.alert({
          message:
            "Trello authorization was not granted, so the card was not marked as read."
        });
        return;
      }

      await rest.del(
        "/cards/" +
          encodeURIComponent(card.id) +
          "/idLabels/" +
          encodeURIComponent(label.id)
      );

      if (typeof t.notifyParent === "function") {
        await t.notifyParent("done");
      }

      await t.alert({
        message: "Marked as read."
      });
    } catch (error) {
      console.error("[Mark Read] Failed:", error);
      await t.alert({
        message: "Could not mark this card as read. Please try again."
      });
    }
  }

  window.TrelloPowerUp.initialize(
    {
      "card-buttons": function (t) {
        return t.card("id", "labels").then(function (card) {
          if (!findNewMessageLabel(card)) {
            return [];
          }

          return [
            {
              icon: ICON_URL,
              text: "Mark Read",
              condition: "edit",
              callback: markRead
            }
          ];
        });
      }
    },
    {
      appKey: POWERUP_APP_KEY,
      appName: POWERUP_APP_NAME
    }
  );
})();
