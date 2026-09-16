window.TrelloPowerUp.initialize({
  "card-buttons": function (t, opts) {
    console.log("MARK READ TEST: card-buttons called", opts);

    return [
      {
        text: "MARK READ TEST"
      }
    ];
  }
});