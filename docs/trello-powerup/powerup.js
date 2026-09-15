(function () {
  "use strict";

  const POWERUP_APP_NAME = "WhatsApp New Message - Mark Read";
  const POWERUP_APP_KEY = "99fb195773ddc1e3bfbd79de31dd8647";

  function markReadButton(t) {
    return t.popup({
      title: "Mark Read",
      url: "./mark-read.html",
      height: 180
    });
  }

  window.TrelloPowerUp.initialize(
    {
      "card-buttons": function () {
        return [
          {
            text: "Mark Read",
            condition: "edit",
            callback: markReadButton
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
