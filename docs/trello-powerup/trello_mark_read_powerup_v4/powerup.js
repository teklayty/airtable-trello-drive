(function () {
  "use strict";

  const POWERUP_APP_NAME = "WhatsApp New Message - Mark Read";
  const POWERUP_APP_KEY = "51604b16c4177e1d0f91ff51797d1348";
  const NEW_MESSAGE_LABEL_NAME = "New Message";

  function showAuthorization(t) {
    return t.popup({
      title: "Authorize Mark Read",
      url: "./authorize.html",
      height: 220
    });
  }

  async function markRead(t) {
    try {
      const card = await t.card("id", "labels");
      const labels = Array.isArray(card.labels) ? card.labels : [];

      const label = labels.find(function (item) {
        return (
          item &&
          typeof item.name === "string" &&
          item.name.trim().toLowerCase() ===
            NEW_MESSAGE_LABEL_NAME.toLowerCase()
        );
      });

      if (!label || !label.id) {
        await t.alert({
          message:
            '"' + NEW_MESSAGE_LABEL_NAME + '" is already absent from this card.'
        });
        return;
      }

      const rest = await t.getRestApi();

      if (!(await rest.isAuthorized())) {
        return showAuthorization(t);
      }

      await rest.del(
        "/cards/" +
          encodeURIComponent(card.id) +
          "/idLabels/" +
          encodeURIComponent(label.id)
      );

      await t.alert({ message: "Marked as read." });

      if (typeof t.notifyParent === "function") {
        await t.notifyParent("done");
      }
    } catch (error) {
      console.error("[WhatsApp Mark Read] error:", error);
      await t.alert({
        message: "Could not mark this card as read. Please try again."
      });
    }
  }

  window.TrelloPowerUp.initialize(
    {
      "card-buttons": function () {
        return [
          {
            text: "Mark Read",
            condition: "edit",
            callback: markRead
          }
        ];
      }
    },
    {
      appKey: POWERUP_APP_KEY,
      appName: POWERUP_APP_NAME
    }
  );
})();
