/*
 * WhatsApp New Message - Mark Read
 * Trello Power-Up
 *
 * The card button is always available to users who can edit the card.
 * Clicking it:
 *   1. Checks whether the "New Message" label exists.
 *   2. Gets the Trello REST API client.
 *   3. If not authorized, opens a user-clicked authorization popup.
 *   4. If authorized, removes the label.
 */

(function () {
  "use strict";

  const POWERUP_APP_NAME = "WhatsApp New Message - Mark Read";
  const POWERUP_APP_KEY = "99fb195773ddc1e3bfbd79de31dd8647";
  const NEW_MESSAGE_LABEL_NAME = "New Message";

  function showAuthorization(t) {
    return t.popup({
      title: "Authorize Mark Read",
      url: "./authorize.html",
      height: 190
    });
  }

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

  function markRead(t) {
    return Promise.resolve()
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
              '" is already absent from this card."
          }).then(function () {
            return null;
          });
        }

        return t.getRestApi().then(function (rest) {
          return rest.isAuthorized().then(function (authorized) {
            if (!authorized) {
              return showAuthorization(t);
            }

            return rest.del(
              "/cards/" +
                encodeURIComponent(card.id) +
                "/idLabels/" +
                encodeURIComponent(label.id)
            ).then(function () {
              return t.alert({ message: "Marked as read." }).then(function () {
                if (typeof t.notifyParent === "function") {
                  return t.notifyParent("done");
                }
                return null;
              });
            });
          });
        });
      })
      .catch(function (error) {
        console.error("[WhatsApp Mark Read] error:", error);

        return t.alert({
          message:
            "Could not mark this card as read. Check the browser console for details."
        });
      });
  }

  window.TrelloPowerUp.initialize(
    {
      "card-buttons": function (t) {
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
