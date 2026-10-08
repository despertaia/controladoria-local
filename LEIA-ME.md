# Controladoria do Lex Lab (Desperta.IA)

A Controladoria acompanha os seus processos no PJe e as publicações no Diário (DJEN),
monta a sua carteira e mostra num painel o que chegou, o que pede ação e o que está
parado. Ela roda **no seu computador**: os dados não saem dele.

## Requisitos

- Windows 10 ou 11, ou macOS 12 ou mais novo.
- O Claude (aplicativo de computador ou Claude Code).
- Login e senha do PJe do seu tribunal (TJMT ou TJMG).

## Como instalar

Abra o Claude e cole esta frase:

> Instale a Controladoria do Lex Lab seguindo https://raw.githubusercontent.com/despertaia/controladoria-local/main/INSTALAR.md

O Claude segue o roteiro de instalação, pede o seu "sim" e faz tudo. Em uns 5 minutos o
painel abre no navegador em http://127.0.0.1:5056. Na tela de **Configuração**, preencha
**você mesmo** o tribunal, o CPF, as senhas do PJe, o seu nome como sai no Diário e a OAB.
Nunca mande senhas pelo chat.

Depois disso, a Controladoria liga sozinha com o computador e confere seus processos às
6h, 12h e 18h nos dias úteis. Para abrir o painel, use o atalho **Controladoria** na Área
de Trabalho.

## Onde ficam as coisas

- Programa e dados: pasta `Controladoria`, dentro da sua pasta de usuário. Os dados ficam
  em `Controladoria/dados` e nunca são tocados por uma atualização.
- Senhas do PJe: no cofre do próprio sistema (Gerenciador de Credenciais no Windows,
  Chaves no Mac). Não ficam em nenhum arquivo.

## Atualizar

Peça ao Claude: "Atualize a Controladoria do Lex Lab". É o mesmo comando da instalação;
dados e senhas são mantidos.

## Desinstalar

Peça ao Claude: "Desinstale a Controladoria do Lex Lab". Por padrão os dados ficam; para
apagar tudo, diga isso explicitamente. A Banca não é afetada.
