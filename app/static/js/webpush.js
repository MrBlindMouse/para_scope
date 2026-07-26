(function () {
  var btn = document.getElementById("webpush-toggle");
  if (!btn || !("serviceWorker" in navigator) || !("PushManager" in window)) {
    if (btn) btn.hidden = true;
    return;
  }

  function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute("content") || "" : "";
  }

  function urlBase64ToUint8Array(base64String) {
    var padding = "=".repeat((4 - (base64String.length % 4)) % 4);
    var base64 = (base64String + padding).replace(/-/g, "+").replace(/_/g, "/");
    var raw = atob(base64);
    var out = new Uint8Array(raw.length);
    for (var i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
    return out;
  }

  function setLabel(subscribed) {
    var label = subscribed ? "Turn notifications off" : "Turn notifications on";
    btn.dataset.subscribed = subscribed ? "1" : "0";
    btn.setAttribute("aria-label", label);
    btn.setAttribute("title", label);
  }

  async function currentSubscription() {
    var reg = await navigator.serviceWorker.getRegistration("/");
    if (!reg) return null;
    return reg.pushManager.getSubscription();
  }

  async function refresh() {
    try {
      var sub = await currentSubscription();
      setLabel(!!sub);
      btn.hidden = false;
    } catch (e) {
      btn.hidden = true;
    }
  }

  async function subscribe() {
    var keyResp = await fetch("/api/push/vapid-public-key");
    if (!keyResp.ok) {
      var err = await keyResp.json().catch(function () { return {}; });
      alert(err.error || "Browser notifications aren’t set up");
      return;
    }
    var keyData = await keyResp.json();
    var reg = await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    await navigator.serviceWorker.ready;
    var sub = await reg.pushManager.subscribe({
      userVisibleOnly: true,
      applicationServerKey: urlBase64ToUint8Array(keyData.public_key),
    });
    var json = sub.toJSON();
    var resp = await fetch("/api/push/subscribe", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": csrfToken(),
      },
      body: JSON.stringify({
        endpoint: json.endpoint,
        keys: json.keys || {},
      }),
    });
    if (!resp.ok) {
      var body = await resp.json().catch(function () { return {}; });
      alert(body.error || "Couldn’t save notification settings");
      return;
    }
    setLabel(true);
  }

  async function unsubscribe() {
    var sub = await currentSubscription();
    if (sub) {
      await fetch("/api/push/subscribe", {
        method: "DELETE",
        headers: {
          "Content-Type": "application/json",
          "X-CSRF-Token": csrfToken(),
        },
        body: JSON.stringify({ endpoint: sub.endpoint }),
      });
      await sub.unsubscribe();
    }
    setLabel(false);
  }

  btn.addEventListener("click", function () {
    if (btn.dataset.subscribed === "1") {
      unsubscribe().catch(function (e) { console.error(e); });
    } else {
      subscribe().catch(function (e) { console.error(e); });
    }
  });

  refresh();
})();
