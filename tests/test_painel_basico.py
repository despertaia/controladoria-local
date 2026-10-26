"""Rede de segurança: o painel importa e responde sem credenciais reais."""

import painel


def test_painel_importa_e_responde_login():
    cliente = painel.app.test_client()
    resposta = cliente.get("/login")
    # Sem PAINEL_SENHA o login fica desligado e redireciona para a lista (302);
    # com senha, mostra o formulário (200).
    assert resposta.status_code in (200, 302)
