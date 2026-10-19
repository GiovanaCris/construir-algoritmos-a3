import os
import sys
import time
import webbrowser
from pathlib import Path

import folium
import requests
from dotenv import load_dotenv
from folium.plugins import Fullscreen

# Carrega o token do arquivo .env
PASTA_PROJETO = Path(__file__).resolve().parent
load_dotenv(PASTA_PROJETO / ".env")

TOKEN = os.getenv("SPTRANS_TOKEN")
URL_BASE = "https://api.olhovivo.sptrans.com.br/v2.1"

LINHAS_CANDIDATAS = ["118Y", "8000", "175T", "477P", "875C"]

TILES_URL = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Street_Map/MapServer/tile/{z}/{y}/{x}"
)
TILES_ATTR = "Tiles &copy; Esri &mdash; Esri, DeLorme, NAVTEQ, USGS, OpenStreetMap contributors"

TENTATIVAS = 3


def obter_json(sessao, endpoint, params=None):
    #Consulta um endpoint da API e devolve a resposta em JSON.
    url = f"{URL_BASE}/{endpoint.lstrip('/')}"
    resposta = sessao.get(url, params=params, timeout=30)
    resposta.raise_for_status()
    return resposta.json()


def obter_lista_com_retentativa(sessao, endpoint, params):
    #A API da SPTrans às vezes devolve lista vazia na primeira chamada
    dados = []
    for tentativa in range(1, TENTATIVAS + 1):
        dados = obter_json(sessao, endpoint, params=params)
        if isinstance(dados, list) and dados:
            return dados
        if tentativa < TENTATIVAS:
            time.sleep(1)
    return dados if isinstance(dados, list) else []


def autenticar():
    #Autentica na API e devolve uma sessão persistente.
    if not TOKEN or TOKEN == "COLE_SEU_TOKEN_AQUI":
        raise ValueError(

        )

    sessao = requests.Session()
    resposta = sessao.post(
        f"{URL_BASE}/Login/Autenticar",
        params={"token": TOKEN},
        timeout=30,
    )
    resposta.raise_for_status()

    if resposta.text.strip().lower() != "true":
        raise PermissionError(
            "A SPTrans não autenticou. Confira se o token está correto "
        )

    print("Autenticação realizada com sucesso.")
    return sessao


def buscar_codigos_linha(sessao, termo):
    #Procura os códigos da linha (um por sentido) pelo letreiro.
    #Devolve (lista de códigos, nome de exibição da linha).
    try:
        dados = obter_json(sessao, "Linha/Buscar", params={"termosBusca": termo})
    except requests.RequestException as erro:
        print(f"  Falha ao buscar '{termo}': {erro}")
        return [], termo

    codigos = []
    nome = termo
    if isinstance(dados, list):
        for linha in dados:
            letreiro = str(linha.get("lt", ""))
            if termo.upper() not in letreiro.upper():
                continue
            codigos.append(linha["cl"])
            if len(codigos) == 1:
                nome = f"{letreiro} - {linha.get('tp')} / {linha.get('ts')}"
            print(
                f"  Linha encontrada: código {linha['cl']} | "
                f"{letreiro}-{linha.get('tl')} | sentido {linha.get('sl')} | "
                f"{linha.get('tp')} -> {linha.get('ts')}"
            )
    return codigos, nome


def buscar_paradas(sessao, codigos):
    #Busca as paradas atendidas pela linha (todos os sentidos), sem duplicar.
    paradas = {}
    for codigo in codigos:
        dados = obter_lista_com_retentativa(
            sessao,
            "Parada/BuscarParadasPorLinha",
            params={"codigoLinha": codigo},
        )
        print(f"  Código {codigo}: {len(dados)} paradas retornadas.")
        for parada in dados:
            if parada.get("py") is None or parada.get("px") is None:
                continue
            paradas[parada.get("cp", id(parada))] = parada
    return list(paradas.values())


def buscar_paradas_por_previsao(sessao, codigos):
    paradas = {}
    for codigo in codigos:
        try:
            dados = obter_json(
                sessao, "Previsao/Linha", params={"codigoLinha": codigo}
            )
        except requests.RequestException as erro:
            print(f"  Previsao/Linha falhou para o código {codigo}: {erro}")
            continue

        if not isinstance(dados, dict):
            print(f"  Previsao/Linha (código {codigo}) formato inesperado: "
                  f"{str(dados)[:200]}")
            continue

        lista = dados.get("ps") or []
        print(f"  Previsao/Linha (código {codigo}): {len(lista)} paradas.")
        for parada in lista:
            if parada.get("py") is None or parada.get("px") is None:
                continue
            paradas[parada.get("cp", id(parada))] = parada
    return list(paradas.values())


def buscar_onibus(sessao, codigos):
    #Busca as posições mais recentes dos ônibus da linha (todos os sentidos).
    veiculos = {}
    horario_api = "não informado"
    for codigo in codigos:
        dados = obter_json(
            sessao,
            "Posicao/Linha",
            params={"codigoLinha": codigo},
        )
        if not isinstance(dados, dict):
            raise ValueError("A API retornou um formato inesperado para as posições.")
        horario_api = dados.get("hr", horario_api)
        for onibus in dados.get("vs", []):
            if onibus.get("py") is None or onibus.get("px") is None:
                continue
            veiculos[onibus.get("p", id(onibus))] = onibus
    return list(veiculos.values()), horario_api


def escolher_linha(sessao):
    #Testa as linhas candidatas e devolve a primeira com paradas.
    #Se nenhuma tiver paradas, devolve a primeira que existir na API.
    reserva = None
    for termo in LINHAS_CANDIDATAS:
        print(f"Testando a linha {termo}...")
        codigos, nome = buscar_codigos_linha(sessao, termo)
        if not codigos:
            print(f"  Linha {termo} não encontrada.")
            continue

        paradas = buscar_paradas(sessao, codigos)
        if not paradas:
            print("  Sem paradas no endpoint principal. Tentando a previsão...")
            paradas = buscar_paradas_por_previsao(sessao, codigos)

        if paradas:
            return codigos, nome, paradas
        print(f"  Linha {termo} sem paradas na API. Tentando a próxima.")
        if reserva is None:
            reserva = (codigos, nome, [])

    if reserva:
        print("Aviso: nenhuma linha candidata retornou paradas.")
        return reserva
    raise ValueError("Nenhuma das linhas candidatas foi encontrada na API.")


def criar_mapa(paradas, onibus, horario_api, nome_linha):
    #Cria o mapa com pins azuis para paradas e vermelhos para ônibus.
    coordenadas = []

    for parada in paradas:
        coordenadas.append([float(parada["py"]), float(parada["px"])])

    for veiculo in onibus:
        coordenadas.append([float(veiculo["py"]), float(veiculo["px"])])

    if coordenadas:
        centro = [
            sum(ponto[0] for ponto in coordenadas) / len(coordenadas),
            sum(ponto[1] for ponto in coordenadas) / len(coordenadas),
        ]
    else:
        # Centro aproximado da cidade de São Paulo, caso a API não retorne pontos.
        centro = [-23.5505, -46.6333]

    mapa = folium.Map(
        location=centro,
        zoom_start=13,
        control_scale=True,
        tiles=TILES_URL,
        attr=TILES_ATTR,
        name="Mapa base (Esri)",
    )
    Fullscreen(position="topright").add_to(mapa)

    # Ajusta o zoom para mostrar todos os pontos.
    if len(coordenadas) > 1:
        mapa.fit_bounds(coordenadas, padding=(30, 30))

    grupo_paradas = folium.FeatureGroup(
        name=f"Paradas de ônibus ({len(paradas)})",
        show=True,
    )
    grupo_onibus = folium.FeatureGroup(
        name=f"Ônibus em tempo real ({len(onibus)})",
        show=True,
    )

    for parada in paradas:
        nome = parada.get("np", "Parada sem nome")
        endereco = parada.get("ed", "Endereço não informado")
        codigo = parada.get("cp", "não informado")

        folium.Marker(
            location=[float(parada["py"]), float(parada["px"])],
            tooltip=nome,
            popup=folium.Popup(
                f"<b>Parada:</b> {nome}<br>"
                f"<b>Endereço:</b> {endereco}<br>"
                f"<b>Código:</b> {codigo}",
                max_width=350,
            ),
            icon=folium.Icon(color="blue", icon="bus", prefix="fa"),
        ).add_to(grupo_paradas)

    for veiculo in onibus:
        prefixo = veiculo.get("p", "não informado")
        acessivel = "Sim" if veiculo.get("a") else "Não"
        horario_posicao = veiculo.get("ta", "não informado")

        folium.Marker(
            location=[float(veiculo["py"]), float(veiculo["px"])],
            tooltip=f"Ônibus {prefixo}",
            popup=folium.Popup(
                f"<b>Veículo:</b> {prefixo}<br>"
                f"<b>Linha:</b> {nome_linha}<br>"
                f"<b>Acessível:</b> {acessivel}<br>"
                f"<b>Horário da posição:</b> {horario_posicao}<br>"
                f"<b>Horário de referência da API:</b> {horario_api}",
                max_width=350,
            ),
            icon=folium.Icon(color="red", icon="bus", prefix="fa"),
        ).add_to(grupo_onibus)

    grupo_paradas.add_to(mapa)
    grupo_onibus.add_to(mapa)
    folium.LayerControl(collapsed=False).add_to(mapa)

    legenda = """
    <div style="
        position: fixed; bottom: 30px; left: 30px; z-index: 9999;
        background: white; padding: 10px 12px; border: 1px solid #888;
        border-radius: 5px; font-size: 13px;">
        <b>Legenda</b><br>
        <span style="color: blue;">●</span> Paradas de ônibus<br>
        <span style="color: red;">●</span> Ônibus em circulação
    </div>
    """
    mapa.get_root().html.add_child(folium.Element(legenda))

    arquivo_mapa = PASTA_PROJETO / "mapa_onibus.html"
    mapa.save(str(arquivo_mapa))
    return arquivo_mapa


def main():
    try:
        sessao = autenticar()

        codigos, nome_linha, paradas = escolher_linha(sessao)
        onibus, horario_api = buscar_onibus(sessao, codigos)

        if not paradas:
            print("Aviso: a API não retornou paradas para nenhuma linha testada.")

        if not onibus:
            print(
                "Aviso: a API não retornou posições de ônibus para essa linha "
                "neste momento. O mapa será criado com as paradas disponíveis."
            )

        arquivo = criar_mapa(paradas, onibus, horario_api, nome_linha)

        print(f"Linha monitorada: {nome_linha} (códigos {codigos})")
        print(f"Paradas encontradas: {len(paradas)}")
        print(f"Ônibus com posição retornada pela API: {len(onibus)}")
        print(f"Mapa salvo em: {arquivo}")

        # Tenta abrir o mapa no navegador padrão.
        try:
            webbrowser.open(arquivo.as_uri())
        except Exception:
            print("Abra manualmente o arquivo mapa_onibus.html no navegador.")

    except (requests.RequestException, ValueError, PermissionError) as erro:
        print(f"Não foi possível concluir a consulta: {erro}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()