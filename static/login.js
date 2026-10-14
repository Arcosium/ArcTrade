'use strict';
const byId = id => document.getElementById(id);
byId('auth-form').onsubmit = async event => {
  event.preventDefault();
  byId('submit').disabled = true;
  byId('message').textContent = '';
  try {
    const response = await fetch('/api/auth/login', {
      method: 'POST',
      headers: {'Content-Type': 'application/json', 'X-Requested-With': 'ArcTrade'},
      body: JSON.stringify({username: byId('username').value.trim(), password: byId('password').value})
    });
    const result = await response.json();
    if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : '아이디와 비밀번호 형식을 확인해 주세요.');
    location.assign('/?view=research');
  } catch (error) {
    byId('message').textContent = error.message || '연결을 확인해 주세요.';
  } finally {
    byId('submit').disabled = false;
  }
};
