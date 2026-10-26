# Desinstala a Controladoria do Lex Lab deste Windows: para o programa, tira o inicio
# automatico e o atalho da Area de Trabalho. Os dados (processos, quadro, pecas) e as senhas
# guardadas no Gerenciador de Credenciais so sao apagados com -ApagarDados.
# A Banca nao e tocada.
#
#   powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Controladoria\local\desinstalar.ps1" [-ApagarDados]
#
# Arquivo todo ASCII de proposito (veja instalar.ps1); acentos via \uXXXX e a funcao T.
param(
    [string]$Destino = '',
    [switch]$ApagarDados
)

$ErrorActionPreference = 'Stop'

function T([string]$texto) { return [regex]::Unescape($texto) }

function Uninstall-Controladoria([string]$Destino, [bool]$Apagar) {
    if (-not $Destino) { $Destino = Join-Path $env:USERPROFILE 'Controladoria' }
    try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch { }
    Write-Host (T '== Controladoria do Lex Lab: desinstala\u00e7\u00e3o ==')

    $procs = Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -and $_.CommandLine -like '*local.iniciar*' }
    foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($procs) { Start-Sleep -Seconds 2 }
    Write-Host (T 'Programa parado.')

    $inicializar = [Environment]::GetFolderPath('Startup')
    $areaDeTrabalho = [Environment]::GetFolderPath('Desktop')
    foreach ($atalho in @((Join-Path $inicializar 'Controladoria.lnk'), (Join-Path $areaDeTrabalho 'Controladoria.lnk'))) {
        if (Test-Path -LiteralPath $atalho) { Remove-Item -LiteralPath $atalho -Force }
    }
    Write-Host (T 'In\u00edcio autom\u00e1tico e atalho da \u00c1rea de Trabalho removidos.')

    if (-not $Apagar) {
        Write-Host ((T 'Seus dados continuam em {0}.') -f $Destino)
        Write-Host (T 'Para apagar tudo (inclusive as senhas guardadas), rode de novo com -ApagarDados.')
        Write-Host (T 'Pronto. A Banca n\u00e3o foi alterada.')
        return
    }
    if (-not (Test-Path -LiteralPath $Destino)) {
        Write-Host (T 'Pronto. A Banca n\u00e3o foi alterada.')
        return
    }
    if (-not (Test-Path -LiteralPath (Join-Path $Destino 'local\iniciar.py'))) {
        throw ((T '{0} n\u00e3o parece a pasta da Controladoria; n\u00e3o apaguei nada.') -f $Destino)
    }
    $py = Join-Path $Destino '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $py) {
        # Senhas e chave guardadas no Gerenciador de Credenciais (nada e mostrado).
        $codigo = 'import keyring' + "`n" +
            'for nome in ("pje_cpf", "pje_senha", "pje_senha_2grau", "secret_key"):' + "`n" +
            '    try: keyring.delete_password("br.com.despertaia.controladoria", nome)' + "`n" +
            '    except Exception: pass'
        $anterior = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $py -c $codigo 2>$null | Out-Null } catch { } finally { $ErrorActionPreference = $anterior }
    }
    Set-Location -LiteralPath $env:USERPROFILE
    Remove-Item -LiteralPath $Destino -Recurse -Force
    Write-Host ((T 'Pasta {0} e dados apagados.') -f $Destino)
    Write-Host (T 'Pronto. A Banca n\u00e3o foi alterada.')
}

try {
    Uninstall-Controladoria $Destino $ApagarDados.IsPresent
} catch {
    Write-Host ((T 'ERRO: {0}') -f $_.Exception.Message) -ForegroundColor Red
    exit 1
}
