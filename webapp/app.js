/* Логика мини-аппа: состояние, экраны, подключение аккаунта.
 *
 * Кто открыл приложение, страница не решает и не сообщает: она лишь
 * пересылает серверу initData — подписанную клиентом Telegram строку.
 * Сервер проверяет подпись токеном бота и берёт user_id оттуда. Поэтому
 * в запросах здесь нет ни одного «свой id», и подделать чужой нельзя.
 */

'use strict';

const tg = window.Telegram && window.Telegram.WebApp;

/** Состояние с сервера: подписка, аккаунты, лимиты. */
let state = null;
/** Незавершённый вход: токен и шаг. */
let login = null;

const $ = (id) => document.getElementById(id);

const SCREENS = [
  'loading', 'main', 'phone', 'code', 'password', 'done', 'outside',
];

function show(name) {
  for (const screen of SCREENS) {
    $('screen-' + screen).hidden = screen !== name;
  }
  // Кнопка «назад» в шапке Telegram: на шагах входа она уводит на
  // главный экран, на главном её быть не должно — иначе она закрывает
  // приложение, и это выглядит как сбой.
  if (tg && tg.BackButton) {
    const inFlow = ['phone', 'code', 'password'].includes(name);
    inFlow ? tg.BackButton.show() : tg.BackButton.hide();
  }
  window.scrollTo(0, 0);
}

function haptic(kind) {
  if (tg && tg.HapticFeedback) {
    if (kind === 'error' || kind === 'success') {
      tg.HapticFeedback.notificationOccurred(kind);
    } else {
      tg.HapticFeedback.impactOccurred('light');
    }
  }
}

function fail(id, text) {
  const box = $(id);
  box.textContent = text;
  box.hidden = !text;
  if (text) haptic('error');
}

/** Запрос к своему API. initData уходит и заголовком, и в теле:
 *  некоторые прокси хостингов срезают нестандартные заголовки. */
async function api(path, body) {
  const initData = (tg && tg.initData) || '';
  const response = await fetch(path, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Init-Data': initData,
    },
    body: JSON.stringify(Object.assign({ init_data: initData }, body || {})),
  });
  let data = {};
  try {
    data = await response.json();
  } catch (error) {
    return { ok: false, error: 'Сервер не ответил. Попробуйте позже.' };
  }
  if (response.status === 401) {
    return { ok: false, error: data.error || 'Откройте приложение из бота заново.' };
  }
  return data;
}

/** Кнопка на время запроса: заблокировать и сказать, что идёт работа. */
function busy(button, text) {
  if (!button.dataset.label) button.dataset.label = button.textContent;
  button.disabled = true;
  button.textContent = text;
  return () => {
    button.disabled = false;
    button.textContent = button.dataset.label;
  };
}

// --- главный экран ----------------------------------------------------

function dateText(stamp) {
  if (!stamp) return '';
  return new Date(stamp * 1000).toLocaleDateString('ru-RU', {
    day: '2-digit', month: '2-digit', year: 'numeric',
  });
}

function plural(n, one, few, many) {
  const tail = Math.abs(n) % 100;
  if (tail >= 11 && tail <= 14) return many;
  const last = tail % 10;
  if (last === 1) return one;
  if (last >= 2 && last <= 4) return few;
  return many;
}

function renderSubscription() {
  const sub = state.subscription;
  const card = $('sub-card');
  const days = sub.days_left;
  card.classList.toggle('expired', !sub.active);

  if (sub.kind === 'paid') {
    $('sub-title').textContent = '💎 Подписка активна';
    $('sub-note').textContent =
      `До ${dateText(sub.until)}. Подпись о боте в сообщениях не добавляется.`;
  } else if (sub.kind === 'trial') {
    $('sub-title').textContent = '🎁 Пробный период';
    $('sub-note').textContent =
      `Осталось ${days} ${plural(days, 'день', 'дня', 'дней')}, до ` +
      `${dateText(sub.until)}. Пока он идёт, в конце каждого сообщения ` +
      'рассылки дописывается строка о том, каким ботом она сделана.';
  } else {
    $('sub-title').textContent = '⌛️ Бесплатный период закончился';
    $('sub-note').textContent =
      'Аккаунты остаются подключёнными. Чтобы продолжить рассылки, ' +
      'напишите в поддержку — включим подписку.';
  }
}

function accountRow(account) {
  const alive = account.status === 'ok';
  const row = document.createElement('div');
  row.className = 'account';

  const top = document.createElement('div');
  top.className = 'account-top';

  const name = document.createElement('span');
  name.className = 'account-name';
  name.textContent = account.name || account.phone;
  top.appendChild(name);

  const badge = document.createElement('span');
  badge.className = 'badge' + (alive ? '' : ' dead');
  badge.textContent = alive ? 'подключён' : 'не работает';
  top.appendChild(badge);

  const phone = document.createElement('span');
  phone.className = 'account-phone';
  phone.textContent = account.username ? '@' + account.username : account.phone;
  top.appendChild(phone);

  row.appendChild(top);

  if (!alive && account.note) {
    const note = document.createElement('p');
    note.className = 'hint small';
    note.textContent = account.note + '. Подключите аккаунт заново.';
    row.appendChild(note);
  }

  const actions = document.createElement('div');
  actions.className = 'account-actions';

  const check = document.createElement('button');
  check.className = 'btn small';
  check.textContent = 'Проверить';
  check.onclick = async () => {
    const done = busy(check, 'Проверяем…');
    const result = await api('/api/account/verify', { id: account.id });
    done();
    if (!result.ok) {
      alertBox(result.error);
      return;
    }
    haptic(result.account.status === 'ok' ? 'success' : 'error');
    await refresh();
  };
  actions.appendChild(check);

  const forget = document.createElement('button');
  forget.className = 'btn small danger';
  forget.textContent = 'Отключить';
  forget.onclick = () => {
    confirmBox(
      `Отключить ${account.name || account.phone}? Сессия будет отозвана ` +
      'в Telegram, рассылки с этого аккаунта прекратятся.',
      async (yes) => {
        if (!yes) return;
        const done = busy(forget, 'Отключаем…');
        const result = await api('/api/account/forget', { id: account.id });
        done();
        if (!result.ok) {
          alertBox(result.error);
          return;
        }
        haptic('success');
        await refresh();
      },
    );
  };
  actions.appendChild(forget);

  row.appendChild(actions);
  return row;
}

function renderAccounts() {
  const box = $('accounts');
  box.textContent = '';
  for (const account of state.accounts) box.appendChild(accountRow(account));
  $('accounts-empty').hidden = state.accounts.length > 0;

  const full = state.accounts.length >= state.limits.max_accounts;
  const button = $('add-account');
  button.disabled = full || !state.mtproto_ready;
  const note = $('limit-note');
  if (!state.mtproto_ready) {
    note.textContent =
      'Подключение аккаунтов пока не настроено на сервере — напишите в поддержку.';
    note.hidden = false;
  } else if (full) {
    note.textContent =
      `Подключено ${state.accounts.length} из ${state.limits.max_accounts}. ` +
      'Отключите ненужный, чтобы добавить новый.';
    note.hidden = false;
  } else {
    note.hidden = true;
  }
}

function renderMain() {
  renderSubscription();
  renderAccounts();
  show('main');
}

async function refresh() {
  const result = await api('/api/state', {});
  if (!result.ok) {
    show('outside');
    $('screen-outside').querySelector('.hint').textContent = result.error;
    return;
  }
  state = result;
  login = result.pending || null;
  renderMain();
}

// --- диалоги ----------------------------------------------------------

/* Свои диалоги Telegram выглядят родными, но есть не во всех версиях
   клиента — старые молча ничего не покажут. Поэтому проверяем и падаем
   на браузерные: сообщение важнее вида. */

function alertBox(text) {
  if (tg && tg.showAlert) tg.showAlert(text);
  else window.alert(text);
}

function confirmBox(text, callback) {
  if (tg && tg.showConfirm) tg.showConfirm(text, callback);
  else callback(window.confirm(text));
}

// --- вход по номеру ---------------------------------------------------

function startFlow() {
  fail('phone-err', '');
  $('phone').value = '';
  show('phone');
  $('phone').focus();
}

async function requestCode() {
  const button = $('phone-next');
  fail('phone-err', '');
  const phone = $('phone').value.trim();
  if (phone.replace(/\D/g, '').length < 10) {
    fail('phone-err', 'Введите номер полностью, с кодом страны.');
    return;
  }
  const done = busy(button, 'Запрашиваем код…');
  const result = await api('/api/login/start', { phone });
  done();
  if (!result.ok) {
    fail('phone-err', result.error);
    return;
  }
  login = { token: result.token, phone: result.phone, stage: 'code' };
  haptic('light');
  openCode();
}

function openCode() {
  fail('code-err', '');
  $('code').value = '';
  $('code-phone').textContent = login.phone;
  show('code');
  $('code').focus();
}

/* Есть ли ещё живой вход. Проверяется перед каждой отправкой, потому
   что обнулиться он может под руками: после ответа с restart мы уводим
   человека к номеру не сразу, а через секунду — чтобы он успел прочесть
   причину, — и в это окно повторное нажатие «Войти» приходило на пустой
   login. */
function loginLost(errorBox) {
  if (login && login.token) return false;
  fail(errorBox, 'Вход уже не активен. Начните с номера заново.');
  setTimeout(startFlow, 1200);
  return true;
}

async function submitCode() {
  const button = $('code-next');
  fail('code-err', '');
  if (loginLost('code-err')) return;
  const code = $('code').value.replace(/\D/g, '');
  if (!code) {
    fail('code-err', 'Введите код из Telegram.');
    return;
  }
  const done = busy(button, 'Входим…');
  const result = await api('/api/login/code', { token: login.token, code });
  done();
  if (!result.ok) {
    fail('code-err', result.error);
    // restart — код или попытка уже мертвы: возвращаем к номеру, иначе
    // человек будет вводить код в форму, которая его больше не примет.
    if (result.restart) {
      login = null;
      setTimeout(startFlow, 1200);
    }
    return;
  }
  if (result.stage === 'password') {
    login.stage = 'password';
    openPassword();
    return;
  }
  finish(result.account);
}

function openPassword() {
  fail('password-err', '');
  $('password').value = '';
  show('password');
  $('password').focus();
}

async function submitPassword() {
  const button = $('password-next');
  fail('password-err', '');
  if (loginLost('password-err')) return;
  const password = $('password').value;
  if (!password) {
    fail('password-err', 'Введите облачный пароль.');
    return;
  }
  const done = busy(button, 'Входим…');
  const result = await api('/api/login/password', { token: login.token, password });
  done();
  // Пароль не остаётся в поле: приложение сворачивают, а не закрывают, и
  // открытый пароль лежал бы в форме до перезагрузки страницы.
  $('password').value = '';
  if (!result.ok) {
    fail('password-err', result.error);
    if (result.restart) {
      login = null;
      setTimeout(startFlow, 1200);
    }
    return;
  }
  finish(result.account);
}

function finish(account) {
  login = null;
  haptic('success');
  $('done-who').textContent = account.username
    ? `${account.name} · @${account.username}`
    : `${account.name} · ${account.phone}`;
  show('done');
}

async function cancelFlow() {
  if (login && login.token) {
    await api('/api/login/cancel', { token: login.token });
  }
  login = null;
  await refresh();
}

// --- запуск -----------------------------------------------------------

function wire() {
  $('add-account').onclick = startFlow;
  $('phone-next').onclick = requestCode;
  $('code-next').onclick = submitCode;
  $('password-next').onclick = submitPassword;
  $('done-back').onclick = refresh;

  for (const button of document.querySelectorAll('[data-back]')) {
    button.onclick = cancelFlow;
  }

  // Enter на телефонной клавиатуре — самый естественный способ
  // отправить короткое поле, и без этого человек ищет кнопку глазами.
  $('phone').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') requestCode();
  });
  $('code').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitCode();
  });
  $('password').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') submitPassword();
  });
  // Код обычно вставляют целиком: как только цифр набралось пять,
  // отправляем сами — лишнее нажатие тут только мешает.
  $('code').addEventListener('input', () => {
    if ($('code').value.replace(/\D/g, '').length === 5) submitCode();
  });

  if (tg && tg.BackButton) tg.BackButton.onClick(cancelFlow);
}

async function boot() {
  if (!tg || !tg.initData) {
    // Ни SDK, ни подписи — значит, страницу открыли не из Telegram.
    // Спрашивать сервер бессмысленно: он ответит отказом по подписи.
    show('outside');
    return;
  }
  tg.ready();
  tg.expand();
  wire();
  await refresh();
  // Вход, начатый до сворачивания приложения, продолжается с того же
  // шага — за кодом человек уходит в другой чат и возвращается.
  if (login && login.stage === 'code') openCode();
  else if (login && login.stage === 'password') openPassword();
}

/** Наружу — для отладки: перезапустить приложение из консоли. */
window.rassylka = { boot, refresh, get state() { return state; } };

boot();
