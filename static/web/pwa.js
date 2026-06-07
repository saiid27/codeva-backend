let deferredInstallPrompt = null;

function installMessage(button, message) {
  const panel = button.closest(".lang-panel") || document;
  const box = panel.querySelector("[data-install-message]");
  if (box) box.textContent = message;
}

function fallbackMessage() {
  const isIos = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (isIos) {
    return "من Safari اضغط مشاركة ثم Add to Home Screen.";
  }
  return "من قائمة المتصفح اختر Add to Home screen.";
}

window.addEventListener("beforeinstallprompt", (event) => {
  event.preventDefault();
  deferredInstallPrompt = event;
  document.querySelectorAll("[data-install-app]").forEach((button) => {
    button.hidden = false;
  });
});

window.addEventListener("appinstalled", () => {
  deferredInstallPrompt = null;
  document.querySelectorAll("[data-install-message]").forEach((box) => {
    box.textContent = "تم تثبيت التطبيق.";
  });
});

document.addEventListener("click", async (event) => {
  const button = event.target.closest("[data-install-app]");
  if (!button) return;

  if (window.matchMedia("(display-mode: standalone)").matches || navigator.standalone) {
    installMessage(button, "التطبيق مثبت بالفعل.");
    return;
  }

  if (!deferredInstallPrompt) {
    installMessage(button, fallbackMessage());
    return;
  }

  deferredInstallPrompt.prompt();
  const choice = await deferredInstallPrompt.userChoice.catch(() => null);
  deferredInstallPrompt = null;
  if (choice?.outcome === "accepted") {
    installMessage(button, "بدأ تثبيت التطبيق.");
  } else {
    installMessage(button, "يمكنك تثبيته لاحقا من نفس الزر.");
  }
});

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js").catch(() => {});
  });
}
