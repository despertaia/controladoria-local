# Instala ou atualiza a Controladoria do Lex Lab (Desperta.IA) neste Windows.
# Pode rodar quantas vezes quiser: na segunda vez, atualiza o codigo e mantem os dados.
#
#   powershell -ExecutionPolicy Bypass -c "irm https://raw.githubusercontent.com/despertaia/controladoria-local/main/local/instalar.ps1 | iex"
#   powershell -ExecutionPolicy Bypass -File local\instalar.ps1 [-Destino PASTA] [-Url ZIP] [-Origem PASTA] [-SemInicioAutomatico]
#
# -Origem instala a partir de uma pasta local (CI e testes), sem baixar nada.
# -SemInicioAutomatico nao cria o inicio automatico nem inicia (CI).
# Via "irm | iex" os parametros podem vir das variaveis CONTROLADORIA_DESTINO e CONTROLADORIA_URL.
#
# Compativel com o Windows PowerShell 5.1. O arquivo e todo ASCII de proposito: com BOM o
# "irm | iex" quebra, e sem BOM o PowerShell 5.1 le acentos errado. Os acentos das mensagens
# vao escritos como \uXXXX e sao convertidos pela funcao T.
param(
    [string]$Destino = '',
    [string]$Url = '',
    [string]$Origem = '',
    [switch]$SemInicioAutomatico
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'

function T([string]$texto) { return [regex]::Unescape($texto) }

function Diga([string]$texto) { Write-Host $texto }

# Roda um programa externo sem que a saida de erro (progresso do uv) vire excecao.
function Invoke-Nativo([string]$Exe, [string[]]$Argumentos) {
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Argumentos | Out-Host
        $codigo = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $anterior
    }
    if ($codigo -ne 0) {
        throw ((T 'O programa {0} terminou com erro (c\u00f3digo {1}).') -f (Split-Path $Exe -Leaf), $codigo)
    }
}

function Get-SaidaNativa([string]$Exe, [string[]]$Argumentos) {
    $anterior = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $saida = & $Exe @Argumentos 2>$null
        return @{ Codigo = $LASTEXITCODE; Saida = ($saida -join "`n") }
    } catch {
        return @{ Codigo = 1; Saida = '' }
    } finally {
        $ErrorActionPreference = $anterior
    }
}

function Stop-Controladoria {
    $procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*local.iniciar*' }
    $parou = $false
    foreach ($p in $procs) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        $parou = $true
    }
    if ($parou) { Start-Sleep -Seconds 2 }
    return $parou
}

function New-Atalho([string]$Caminho, [string]$Alvo, [string]$Argumentos, [string]$Pasta, [string]$Descricao) {
    $shell = New-Object -ComObject WScript.Shell
    $atalho = $shell.CreateShortcut($Caminho)
    $atalho.TargetPath = $Alvo
    $atalho.Arguments = $Argumentos
    $atalho.WorkingDirectory = $Pasta
    $atalho.WindowStyle = 7
    $atalho.Description = $Descricao
    $atalho.Save()
}

function Find-Uv {
    $cmd = Get-Command uv -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Path }
    $candidatos = @(
        (Join-Path $env:USERPROFILE '.local\bin\uv.exe'),
        (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe')
    )
    foreach ($c in $candidatos) { if (Test-Path -LiteralPath $c) { return $c } }
    return $null
}

# Python 3.12 oficial (python.org), com assinatura digital da Python Software Foundation.
# O Smart App Control do Windows 11 bloqueia programa sem assinatura (como o Python que o
# uv baixa): por isso o oficial vem primeiro e o uv fica de reserva.
$PythonOficialVersao = '3.12.10'  # a ultima 3.12 com instalador para Windows

function Test-AssinadoPSF([string]$Exe) {
    try {
        $assinatura = Get-AuthenticodeSignature -LiteralPath $Exe
        return ($assinatura.Status -eq 'Valid' -and $assinatura.SignerCertificate.Subject -match 'Python Software Foundation')
    } catch { return $false }
}

function Find-PythonOficial {
    $candidatos = @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:ProgramFiles 'Python312\python.exe')
    )
    $py = Get-Command py -ErrorAction SilentlyContinue
    if ($py) {
        $r = Get-SaidaNativa $py.Path @('-3.12', '-c', 'import sys; print(sys.executable)')
        if ($r.Codigo -eq 0 -and $r.Saida.Trim()) { $candidatos = @($r.Saida.Trim()) + $candidatos }
    }
    foreach ($c in $candidatos) {
        if ($c -and (Test-Path -LiteralPath $c) -and (Test-AssinadoPSF $c)) { return $c }
    }
    return $null
}

function Install-PythonOficial([string]$Pasta) {
    $arquitetura = 'amd64'
    if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { $arquitetura = 'arm64' }
    $url = "https://www.python.org/ftp/python/$PythonOficialVersao/python-$PythonOficialVersao-$arquitetura.exe"
    $instalador = Join-Path $Pasta "python-$PythonOficialVersao-$arquitetura.exe"
    Diga (T 'Baixando o Python 3.12 oficial (python.org)...')
    Invoke-WebRequest -Uri $url -OutFile $instalador -UseBasicParsing
    if (-not (Test-AssinadoPSF $instalador)) { throw (T 'O instalador do Python baixado n\u00e3o tem a assinatura da Python Software Foundation.') }
    Diga (T 'Instalando o Python 3.12 oficial (s\u00f3 para este usu\u00e1rio)...')
    $argumentos = '/quiet InstallAllUsers=0 Include_launcher=0 Include_test=0 Include_doc=0 Include_tcltk=0 Shortcuts=0 PrependPath=0 AssociateFiles=0'
    $processo = Start-Process -FilePath $instalador -ArgumentList $argumentos -Wait -PassThru
    if ($processo.ExitCode -ne 0) { throw ((T 'O instalador do Python terminou com erro (c\u00f3digo {0}).') -f $processo.ExitCode) }
    return (Find-PythonOficial)
}

# Versao gravada no arquivo VERSAO de uma pasta ('' se nao houver).
function Get-VersaoDe([string]$Pasta) {
    $arquivo = Join-Path $Pasta 'VERSAO'
    if (-not (Test-Path -LiteralPath $arquivo)) { return '' }
    try { return ([string](Get-Content -LiteralPath $arquivo -Raw)).Trim() } catch { return '' }
}

function Write-Versao([string]$Antiga, [string]$Nova) {
    if (-not $Nova) { return }
    if ($Antiga -and $Antiga -ne $Nova) {
        Diga ((T 'Controladoria atualizada da vers\u00e3o {0} para a {1}.') -f $Antiga, $Nova)
    } else {
        Diga ((T 'Controladoria vers\u00e3o {0} instalada.') -f $Nova)
    }
}

function Install-Controladoria([string]$Destino, [string]$Url, [string]$Origem, [bool]$InicioAutomatico) {
    $script:Passo = T 'preparar'
    if ($env:OS -ne 'Windows_NT') { throw (T 'Este instalador \u00e9 para Windows. No Mac, use o instalar.sh.') }
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

    if (-not $Destino) {
        if ($env:CONTROLADORIA_DESTINO) { $Destino = $env:CONTROLADORIA_DESTINO }
        else { $Destino = Join-Path $env:USERPROFILE 'Controladoria' }
    }
    if (-not $Url) {
        if ($env:CONTROLADORIA_URL) { $Url = $env:CONTROLADORIA_URL }
        else { $Url = 'https://github.com/despertaia/controladoria-local/archive/refs/heads/main.zip' }
    }

    Diga (T '== Controladoria do Lex Lab (Desperta.IA): instala\u00e7\u00e3o ==')
    Diga ((T 'Pasta: {0}') -f $Destino)

    # 1) Codigo novo numa pasta temporaria.
    $tmp = Join-Path ([IO.Path]::GetTempPath()) ('controladoria-' + [guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $tmp -Force | Out-Null
    if ($Origem) {
        $script:Passo = T 'ler a pasta de origem'
        $fonte = (Resolve-Path -LiteralPath $Origem).Path
        if (-not (Test-Path -LiteralPath (Join-Path $fonte 'local\iniciar.py'))) {
            throw ((T '{0} n\u00e3o parece a Controladoria.') -f $fonte)
        }
    } else {
        $script:Passo = T 'baixar a Controladoria'
        Diga (T 'Baixando a vers\u00e3o mais recente...')
        $zip = Join-Path $tmp 'controladoria.zip'
        Invoke-WebRequest -Uri $Url -OutFile $zip -UseBasicParsing
        $script:Passo = T 'abrir o arquivo baixado'
        $pastaZip = Join-Path $tmp 'zip'
        Expand-Archive -LiteralPath $zip -DestinationPath $pastaZip -Force
        $fonte = (Get-ChildItem -LiteralPath $pastaZip -Directory | Select-Object -First 1).FullName
        if (-not $fonte -or -not (Test-Path -LiteralPath (Join-Path $fonte 'local\iniciar.py'))) {
            throw (T 'O arquivo baixado n\u00e3o tem a Controladoria.')
        }
    }

    # Versao: a que ja estava instalada (se houver) e a que vai entrar.
    $versaoAntiga = Get-VersaoDe $Destino
    $versaoNova = Get-VersaoDe $fonte

    # 2) Para a versao em uso antes de trocar o codigo (atualizacao).
    $script:Passo = T 'parar a vers\u00e3o em uso'
    if (Stop-Controladoria) { Diga (T 'Vers\u00e3o anterior parada.') }

    # 3) Copia o codigo por cima, sem tocar nos dados.
    $script:Passo = T 'copiar os arquivos'
    New-Item -ItemType Directory -Path $Destino -Force | Out-Null
    $destAbs = (Resolve-Path -LiteralPath $Destino).Path
    $preservar = @('dados', 'peticoes', 'cache', 'grupos', '.venv', '.env', '.git', '.pytest_cache', '__pycache__')
    if ($fonte.TrimEnd('\') -ne $destAbs.TrimEnd('\')) {
        foreach ($item in (Get-ChildItem -LiteralPath $fonte -Force)) {
            if ($preservar -contains $item.Name) { continue }
            $alvo = Join-Path $destAbs $item.Name
            if (Test-Path -LiteralPath $alvo) { Remove-Item -LiteralPath $alvo -Recurse -Force }
            Copy-Item -LiteralPath $item.FullName -Destination $alvo -Recurse -Force
        }
    }
    New-Item -ItemType Directory -Path (Join-Path $destAbs 'dados') -Force | Out-Null
    Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Diga (T 'Arquivos copiados.')

    # 4) e 5) Python 3.12 e dependencias. Primeiro o Python oficial (assinado); se nao
    # der, o uv, como antes.
    $script:Passo = T 'preparar o Python'
    $venv = Join-Path $destAbs '.venv'
    $py = Join-Path $venv 'Scripts\python.exe'
    $pyw = Join-Path $venv 'Scripts\pythonw.exe'
    $pronto = $false
    if (Test-Path -LiteralPath $py) {
        $r = Get-SaidaNativa $py @('-c', 'import sys; sys.exit(sys.version_info[:2] != (3, 12))')
        $pronto = ($r.Codigo -eq 0)
    }
    $uv = $null
    if ($pronto) {
        Diga (T 'Python 3.12 j\u00e1 preparado.')
        $uv = Find-Uv
    } else {
        if (Test-Path -LiteralPath $venv) { Remove-Item -LiteralPath $venv -Recurse -Force }
        $oficial = $null
        if (-not $env:CONTROLADORIA_SEM_PYTHON_OFICIAL) {
            try {
                if (-not $env:CONTROLADORIA_BAIXAR_PYTHON) { $oficial = Find-PythonOficial }  # CI: forca o download
                if (-not $oficial) {
                    $tmpPy = Join-Path ([IO.Path]::GetTempPath()) ('controladoria-py-' + [guid]::NewGuid().ToString('N'))
                    New-Item -ItemType Directory -Path $tmpPy -Force | Out-Null
                    try { $oficial = Install-PythonOficial $tmpPy } finally { Remove-Item -LiteralPath $tmpPy -Recurse -Force -ErrorAction SilentlyContinue }
                }
            } catch {
                Diga ((T 'N\u00e3o deu para usar o Python oficial ({0}); seguindo pelo uv.') -f $_.Exception.Message)
                $oficial = $null
            }
        }
        Diga (T 'Preparando o Python 3.12 (pode levar alguns minutos na primeira vez)...')
        if ($oficial) {
            Invoke-Nativo $oficial @('-m', 'venv', $venv)
        } else {
            $script:Passo = T 'instalar o uv'
            $uv = Find-Uv
            if (-not $uv) {
                Diga (T 'Instalando o uv (gerenciador de Python)...')
                $env:INSTALLER_NO_MODIFY_PATH = '1'
                Invoke-Nativo 'powershell' @('-NoProfile', '-ExecutionPolicy', 'ByPass', '-c', 'irm https://astral.sh/uv/install.ps1 | iex')
                $uv = Find-Uv
            }
            if (-not $uv) { throw (T 'N\u00e3o encontrei o uv depois de instalar.') }
            $script:Passo = T 'preparar o Python'
            Push-Location -LiteralPath $destAbs
            try { Invoke-Nativo $uv @('venv', '--python', '3.12', '.venv') } finally { Pop-Location }
        }
    }
    $script:Passo = T 'instalar as depend\u00eancias'
    Diga (T 'Instalando as depend\u00eancias...')
    $requisitos = Join-Path $destAbs 'requirements-local.txt'
    $temPip = (Get-SaidaNativa $py @('-m', 'pip', '--version')).Codigo -eq 0
    if ($temPip) {
        Invoke-Nativo $py @('-m', 'pip', 'install', '--disable-pip-version-check', '--no-input', '-q', '-r', $requisitos)
    } else {
        # venv criado pelo uv (sem pip dentro): continua pelo uv
        if (-not $uv) { $uv = Find-Uv }
        if (-not $uv) { throw (T 'N\u00e3o encontrei o pip nem o uv para instalar as depend\u00eancias.') }
        Invoke-Nativo $uv @('pip', 'install', '--python', $py, '-r', $requisitos)
    }
    if (-not (Test-Path -LiteralPath $pyw)) { throw ((T 'N\u00e3o encontrei {0}.') -f $pyw) }

    # 6) Atalho na Area de Trabalho: inicia (se preciso) e abre o painel.
    $script:Passo = T 'criar o atalho na \u00c1rea de Trabalho'
    Push-Location -LiteralPath $destAbs
    try { $r = Get-SaidaNativa $py @('-c', 'from local import iniciar; print(iniciar.ler_porta(None))') } finally { Pop-Location }
    $porta = 5056
    if ($r.Codigo -eq 0 -and ($r.Saida.Trim() -match '^\d+$')) { $porta = [int]$r.Saida.Trim() }
    $urlPainel = "http://127.0.0.1:$porta"
    $areaDeTrabalho = [Environment]::GetFolderPath('Desktop')
    if ($areaDeTrabalho -and (Test-Path -LiteralPath $areaDeTrabalho)) {
        New-Atalho (Join-Path $areaDeTrabalho 'Controladoria.lnk') $pyw '-m local.iniciar --abrir' $destAbs (T 'Abre a Controladoria do Lex Lab no navegador')
        Diga (T 'Atalho "Controladoria" criado na \u00c1rea de Trabalho.')
    }

    if (-not $InicioAutomatico) {
        Diga ''
        Diga (T 'Instalada sem in\u00edcio autom\u00e1tico. Para iniciar:')
        Diga ('  cd "{0}"; .venv\Scripts\python.exe -m local.iniciar --abrir' -f $destAbs)
        Write-Versao $versaoAntiga $versaoNova
        return
    }

    # 7) Inicio automatico: atalho na pasta Inicializar (sem janela, sem administrador).
    $script:Passo = T 'ligar o in\u00edcio autom\u00e1tico'
    $inicializar = [Environment]::GetFolderPath('Startup')
    New-Atalho (Join-Path $inicializar 'Controladoria.lnk') $pyw '-m local.iniciar --sem-navegador' $destAbs (T 'Controladoria do Lex Lab (in\u00edcio autom\u00e1tico)')
    Diga (T 'In\u00edcio autom\u00e1tico ligado.')

    # 8) Inicia e espera o painel responder.
    $script:Passo = T 'iniciar a Controladoria'
    Start-Process -FilePath $pyw -ArgumentList @('-m', 'local.iniciar', '--sem-navegador') -WorkingDirectory $destAbs
    $script:Passo = T 'conferir se o painel subiu'
    $subiu = $false
    # Na primeira vez o Windows (e o antivirus) pode demorar a liberar o Python novo.
    for ($i = 0; $i -lt 180; $i++) {
        try {
            $resposta = Invoke-WebRequest -Uri "$urlPainel/saude" -UseBasicParsing -TimeoutSec 2
            if ($resposta.StatusCode -eq 200) { $subiu = $true; break }
        } catch { }
        if ($i -eq 30) { Diga (T 'Ainda iniciando... na primeira vez pode levar at\u00e9 3 minutos.') }
        Start-Sleep -Seconds 1
    }
    if (-not $subiu) {
        Diga ''
        Diga (T 'AVISO: a Controladoria foi instalada, mas ainda est\u00e1 iniciando.')
        Diga ((T 'Espere 2 minutos e abra o atalho Controladoria. Registro: {0}') -f (Join-Path $destAbs 'dados\controladoria.log'))
        Write-Versao $versaoAntiga $versaoNova
        return
    }
    Diga ''
    Diga ((T 'Pronto! A Controladoria est\u00e1 rodando em: {0}') -f $urlPainel)
    Diga (T 'Abra esse endere\u00e7o no navegador (ou o atalho Controladoria na \u00c1rea de Trabalho).')
    Write-Versao $versaoAntiga $versaoNova
    try { Start-Process $urlPainel } catch { }
}

$script:Passo = T 'preparar'
try {
    Install-Controladoria $Destino $Url $Origem (-not $SemInicioAutomatico)
} catch {
    Write-Host ''
    Write-Host ((T 'ERRO: a instala\u00e7\u00e3o parou na etapa "{0}". Nada dos seus dados foi apagado.') -f $script:Passo) -ForegroundColor Red
    Write-Host ((T 'Detalhe: {0}') -f $_.Exception.Message) -ForegroundColor Red
    Write-Host (T 'Se precisar de ajuda, copie as mensagens acima.')
    exit 1
}
