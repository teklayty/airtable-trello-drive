/*
 * WhatsApp New Message - Mark Read
 * Trello Power-Up
 */

(function () {
  "use strict";

  const POWERUP_APP_NAME = "WhatsApp New Message - Mark Read";
  const POWERUP_APP_KEY = "99fb195773ddc1e3bfbd79de31dd8647";
  const NEW_MESSAGE_LABEL_NAME = "New Message";
  const ICON_URL = "./icon.svg";

  function findNewMessageLabel(card) {
    const labels = Array.isArray(card && card.labels) ? card.labels : [];
    return (
      labels.find(function (label) {
        return (
          label &&
          typeof label.name === "string" &&
          label.name.trim().toLowerCase() ===
            NEW_MESSAGE_LABEL_NAME.toLowerCase()
        );
      }) || null
    );
  }

  async function markRead(t) {
    try {
      const card = await t.card("id", "labels");
      const label = findNewMessageLabel(card);

      if (!label || !label.id) {
        await t.alert({
          message:
            '"' +
            NEW_MESSAGE_LABEL_NAME +
            '" is already absent from this card.',
        });
        return;
      }

      const rest = t.getRestApi();

      if (!(await rest.isAuthorized())) {
        await rest.authorize({ scope: "read,write" });
      }

      if (!(await rest.isAuthorized())) {
        await t.alert({
          message:
            "Trello authorization was not granted, so the card was not marked as read.",
        });
        return;
      }

      await rest.del(
        "/cards/" +
          encodeURIComponent(card.id) +
          "/idLabels/" +
          encodeURIComponent(label.id)
      );

      await t.alert({ message: "Marked as read." });

      if (typeof t.notifyParent === "function") {
        t.notifyParent("done");
      }
    } catch (error) {
      console.error("[WhatsApp Mark Read] error:", error);
      await t.alert({
        message: "Could not mark this card as read. Please try again.",
      });
    }
  }

  window.TrelloPowerUp.initialize(
    {
      "card-buttons": function (t, opts) {
        if (
          typeof t.memberCanWriteToModel === "function" &&
          !t.memberCanWriteToModel("card")
        ) {
          return [];
        }

        return [
          {
            icon: ICON_URL,
            text: "Mark Read",
            condition: "edit",
            callback: markRead,
          },
        ];
      },
    },
    {
      appKey: POWERUP_APP_KEY,
      appName: POWERUP_APP_NAME,
    }
  );
})();
