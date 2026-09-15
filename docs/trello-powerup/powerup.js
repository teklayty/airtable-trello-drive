/*
 * WhatsApp New Message - Mark Read
 * V7
 *
 * Diagnostic/working version.
 * The Mark Read button is shown to editable users.
 * Clicking it gives immediate feedback, checks for the New Message label,
 * authorizes the Power-Up if necessary, and removes the label when authorized.
 */

(function () {
  "use strict";

  const POWERUP_APP_NAME = "WhatsApp New Message - Mark Read";
  const POWERUP_APP_KEY = "99fb195773ddc1e3bfbd79de31dd8647";
  const NEW_MESSAGE_LABEL_NAME = "New Message";

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

  function openAuthorization(t) {
    return t.popup({
      title: "Authorize Mark Read",
      url: "./authorize.html",
      height: 210
    });
  }

  function markRead(t) {
    return t
      .alert({ message: "Mark Read clicked." })
      .then(function () {
        return t.card("id", "labels");
      })
      .then(function (card) {
        const label = findNewMessageLabel(card);

        if (!label || !label.id) {
          return t.alert({
            message:
              '"' +
              NEW_MESSAGE_LABEL_NAME +
              '" is not on this card.'
          });
        }

        return t.getRestApi().then(function (rest) {
          return rest.isAuthorized().then(function (authorized) {
            if (!authorized) {
              return openAuthorization(t);
            }

            return rest
              .del(
                "/cards/" +
                  encodeURIComponent(card.id) +
                  "/idLabels/" +
                  encodeURIComponent(label.id)
              )
              .then(function () {
                return t.alert({ message: "Marked as read." });
              });
          });
        });
      })
      .catch(function (error) {
        console.error("[WhatsApp Mark Read] error:", error);

        return t.alert({
          message:
            "Mark Read failed: " +
            (error && error.message ? error.message : String(error))
        });
      });
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
