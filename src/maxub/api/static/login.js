"use strict";

// Авторизация аккаунта: по коду на телефон и по QR-коду, плюс отключение.
//
// Картинку QR рисует демон и отдаёт готовым data-URI: сторонние скрипты
// запрещены, страница обязана работать без интернета.

const QR_POLL_MS = 2000;

let qrTimer = null;
let phoneChallenge = null;

function openPanel(title, note) {
  stopQr();
  $("panel").hidden = false;
  $("panel-title").textContent = title;
  $("panel-note").textContent = note;
  $("panel-qr").hidden = true;
  $("code-form").hidden = true;
  $("code").value = "";
}

function closePanel() {
  stopQr();
  $("panel").hidden = true;
  phoneChallenge = null;
}

function stopQr() {
  clearInterval(qrTimer);
  qrTimer = null;
}

async function startPhone(account) {
  try {
    const started = await api("POST", "/web/api/login/start", { account_id: account.id });
    phoneChallenge = started.challenge_id;
    openPanel(`Вход по коду: ${describe(account)}`, "Введите код, присланный на телефон.");
    $("code-form").hidden = false;
    $("code").focus();
    say("");
  } catch (error) {
    say(error.message, "error");
  }
}

async function startQr(account) {
  try {
    const started = await api("POST", "/web/api/login/qr/start", { account_id: account.id });
    openPanel(
      `Вход по QR-коду: ${describe(account)}`,
      "Отсканируйте код приложением MAX на телефоне.",
    );
    $("panel-qr").src = started.image;
    $("panel-qr").hidden = false;
    say("");
    qrTimer = setInterval(() => pollQr(started.challenge_id), QR_POLL_MS);
  } catch (error) {
    say(error.message, "error");
  }
}

async function pollQr(challengeId) {
  let polled;
  try {
    polled = await api("POST", "/web/api/login/qr/poll", { challenge_id: challengeId });
  } catch (error) {
    stopQr();
    say(error.message, "error");
    return;
  }
  if (polled.status === "confirmed") {
    closePanel();
    say("Вход подтверждён, аккаунт подключается.", "ok");
    refresh();
  } else if (polled.status === "expired") {
    stopQr();
    $("panel-note").textContent = "Срок действия кода истёк — запросите новый.";
    say("Запрос QR-входа истёк.", "error");
  }
}

async function disable(account) {
  const reason = window.prompt("Причина отключения", "остановлен вручную");
  if (reason === null) return;
  try {
    await api("POST", `/web/api/accounts/${account.id}/disable`, { reason });
    say(`Аккаунт ${describe(account)} отключён.`, "ok");
    refresh();
  } catch (error) {
    say(error.message, "error");
  }
}

$("code-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const account = await api("POST", "/web/api/login/complete", {
      challenge_id: phoneChallenge,
      code: $("code").value.trim(),
    });
    closePanel();
    say(`Аккаунт ${describe(account)}: вход выполнен.`, "ok");
    refresh();
  } catch (error) {
    say(error.message, "error");
  }
});

$("panel-close").addEventListener("click", closePanel);
