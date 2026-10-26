/* Botão "olho" para mostrar/ocultar a senha enquanto se digita.
 *
 * Sem JS: o campo continua um <input type="password"> normal e o botão não existe, porque
 * é este script que o cria (ele envolve cada campo de senha num <span class="campo-senha">
 * e acrescenta o botão). Assim não há botão morto na página.
 * Ao enviar o formulário (ou voltar pelo cache do navegador) o campo volta a "password",
 * para o navegador nunca guardar a senha como texto comum. */
(function () {
  var OLHO = '<svg class="ico-ver" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    '<path d="M1 12s4-7 11-7 11 7 11 7-4 7-11 7-11-7-11-7z"/><circle cx="12" cy="12" r="3"/></svg>';
  var OLHO_CORTADO = '<svg class="ico-ocultar" viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">' +
    '<path d="M17.94 17.94A10.9 10.9 0 0 1 12 19C5 19 1 12 1 12a19.8 19.8 0 0 1 5.06-5.94"/>' +
    '<path d="M9.9 4.24A10.1 10.1 0 0 1 12 4c7 0 11 8 11 8a19.9 19.9 0 0 1-3.17 4.19"/>' +
    '<path d="M14.12 14.12A3 3 0 1 1 9.88 9.88"/><line x1="1" y1="1" x2="23" y2="23"/></svg>';

  function definir(campo, botao, visivel) {
    campo.type = visivel ? 'text' : 'password';
    botao.setAttribute('aria-pressed', visivel ? 'true' : 'false');
    botao.setAttribute('aria-label', visivel ? 'Ocultar senha' : 'Mostrar senha');
  }

  function melhorar(campo) {
    if (campo.dataset.olho) return;
    campo.dataset.olho = '1';
    var caixa = document.createElement('span');
    caixa.className = 'campo-senha';
    var botao = document.createElement('button');
    botao.type = 'button';
    botao.className = 'olho-senha';
    botao.setAttribute('aria-label', 'Mostrar senha');
    botao.setAttribute('aria-pressed', 'false');
    if (campo.id) botao.setAttribute('aria-controls', campo.id);
    botao.innerHTML = OLHO + OLHO_CORTADO;
    campo.parentNode.insertBefore(caixa, campo);
    caixa.appendChild(campo);
    caixa.appendChild(botao);

    botao.addEventListener('click', function () {
      var ini = null, fim = null, dir = 'none';
      try { ini = campo.selectionStart; fim = campo.selectionEnd; dir = campo.selectionDirection || 'none'; } catch (e) {}
      definir(campo, botao, campo.type === 'password');
      campo.focus();
      try { if (ini !== null) campo.setSelectionRange(ini, fim, dir); } catch (e) {}
    });
    if (campo.form) {
      campo.form.addEventListener('submit', function () { definir(campo, botao, false); });
    }
    window.addEventListener('pageshow', function () { definir(campo, botao, false); });
  }

  function iniciar() {
    var campos = document.querySelectorAll('input[type="password"]');
    for (var i = 0; i < campos.length; i++) melhorar(campos[i]);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', iniciar);
  else iniciar();
})();
