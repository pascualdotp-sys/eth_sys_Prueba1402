import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
import os

# ================= CONFIGURACIÓN DE PÁGINA =================
st.set_page_config(page_title="Simulador de Etanol", layout="wide")

# ================= FUNCIONES DE SIMULACIÓN =================
def generar_reporte(sistema):
    # ------ PARTE 1: TABLA DE CORRIENTES -------
    datos_mat = []
    for s in sistema.streams:
        if s.F_mass > 0:
            datos_mat.append({
                "ID Corriente": s.ID,
                "Temp(°C)": f"{s.T-273.15:.2f}",
                "Presion(bar)": f"{s.P/1e5:.2f}",
                "Flujo(kg/h)": f"{s.F_mass:.2f}",
                "% Etanol": f"{s.imass['Ethanol']/s.F_mass:.1%}",
                "% Agua": f"{s.imass['Water']/s.F_mass:.1%}"
            })
    df_mat = pd.DataFrame(datos_mat).set_index("ID Corriente")

    # ----- PARTE 2: TABLA DE ENERGÍA -----
    datos_en = []
    for u in sistema.units:
        calor_kw = 0.0
        tipo_servicio = "-"
        
        try:
            # Caso especial: HXprocess (Recuperación interna)
            if isinstance(u, bst.HXprocess):
                calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
                tipo_servicio = "Recuperacion Interna"
            # Caso especial: Tanque Flash (Prevención del error .duty)
            elif isinstance(u, bst.Flash):
                calor_kw = getattr(u, 'Hnet', 0) / 3600
                tipo_servicio = "Separación Flash"
            # Caso estándar: Equipos con duty
            elif hasattr(u, "duty") and u.duty is not None:
                calor_kw = u.duty / 3600
                if calor_kw > 0.01: tipo_servicio = "Calentamiento (Vapor)"
                if calor_kw < -0.01: tipo_servicio = "Enfriamiento (Agua)"
        except Exception:
            calor_kw = 0.0

        # Potencia Eléctrica
        potencia = getattr(u.power_utility, 'rate', 0.0) if hasattr(u, "power_utility") and u.power_utility else 0.0

        # Solo agregar si consume energía relevante
        if abs(calor_kw) > 0.01:
            datos_en.append({
                "ID Equipo": u.ID,
                "Función": tipo_servicio,
                "Energia Térmica (kw)": f"{calor_kw:.2f}",
            })
        if potencia > 0.01:
            datos_en.append({
                "ID Equipo": u.ID,
                "Función": "Motor bomba",
                "Energia electrica (kw)": f"{potencia:.2f}"
            })

    df_en = pd.DataFrame(datos_en).set_index("ID Equipo")
    return df_mat, df_en

def ejecutar_simulacion(agua_feed, etanol_feed, temp_feed, temp_w220, presion_v100):
    # 🔴 CLAVE: Limpiar el flowsheet para evitar error de "ID duplicado" en Streamlit
    bst.main_flowsheet.clear()
    
    # 1. Configuración termodinámica
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)
    
    # 2. Definición de Corrientes
    mosto = bst.Stream("1-MOSTO",
                       Water=agua_feed, Ethanol=etanol_feed, units="kg/h",
                       T=temp_feed + 273.15, P=101325)
    
    vinazas_retorno = bst.Stream("Vinazas-Retorno",
                                 Water=200, Ethanol=0, units="kg/h",
                                 T=95 + 273.15, P=300000)
    
    # 3. Equipos
    P100 = bst.Pump("P-100", ins=mosto, P=4*101325)
    
    W210 = bst.HXprocess("W-210",
                         ins=(P100-0, vinazas_retorno),
                         outs=("3-Mosto-Pre", "Drenaje"),
                         phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15
    
    # Se inyecta la temperatura variable de salida del W220
    W220 = bst.HXutility("W-220", ins=W210-0, outs="Mezcla", T=temp_w220 + 273.15)
    
    # Se inyecta la presión variable a la válvula V100 (y al Flash V1 por consistencia termodinámica)
    V100 = bst.IsenthalpicValve("V-100", ins=W220-0, outs="Mezcla-Bifasica", P=presion_v100)
    
    V1 = bst.Flash("V-1", ins=V100-0, outs=("Vapor caliente", "Vinazas"), P=presion_v100, Q=0)
    
    W310 = bst.HXutility("W-310", ins=V1-0, outs="Producto Final", T=25 + 273.15)
    
    P200 = bst.Pump("P-200", ins=V1-1, outs=vinazas_retorno, P=3*101325)
    
    # 4. Simulación
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    eth_sys.simulate()
    
    # 5. Reportes
    df_mat, df_en = generar_reporte(eth_sys)
    
    # 6. Generar diagrama (Mantenido al final del script)
    diagram_path = "diagrama_etanol_final"
    eth_sys.diagram(file=diagram_path, format="png")
    
    return df_mat, df_en, diagram_path + ".png"


# ================= INTERFAZ STREAMLIT =================
st.title("🏭 Simulador de Planta de Etanol")

st.sidebar.header("Parámetros de Operación")

# --- SLIDERS OBLIGATORIOS ---
# 1. Slider para la temperatura de alimentación del mosto
temp_feed = st.sidebar.slider("Temperatura de alimentación del mosto (°C)", min_value=10, max_value=80, value=25)

# 2. Slider para la temperatura de salida del W-220
temp_w220 = st.sidebar.slider("Temperatura de salida del calentador W-220 (°C)", min_value=80, max_value=120, value=92)

# 3. Slider para la presión del separador V-100 (Mostrado en atmósferas y convertido a Pascales)
presion_v100_atm = st.sidebar.slider("Presión del separador V-100 (atm)", min_value=0.1, max_value=3.0, value=1.0)
presion_v100_pa = presion_v100_atm * 101325 

# --- SLIDERS OPCIONALES (Flujos) ---
st.sidebar.markdown("---")
st.sidebar.subheader("Flujos de Alimentación")
agua_feed = st.sidebar.slider("Flujo de Agua (kg/h)", 500, 1500, 900)
etanol_feed = st.sidebar.slider("Flujo de Etanol (kg/h)", 50, 500, 100)

if st.sidebar.button("Simular Proceso"):
    with st.spinner("Calculando balances y generando diagrama..."):
        try:
            # Pasamos las variables directamente a la función
            df_mat, df_en, diagram_file = ejecutar_simulacion(agua_feed, etanol_feed, temp_feed, temp_w220, presion_v100_pa)
            
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("Balance de Materia")
                st.dataframe(df_mat)
            with col2:
                st.subheader("Balance de Energía")
                st.dataframe(df_en)
                
            st.subheader("Diagrama de Flujo (PFD)")
            if os.path.exists(diagram_file):
                st.image(diagram_file)
            else:
                st.warning("El diagrama no se pudo renderizar. Verifica que graphviz esté instalado en el sistema operativo.")
            
            # Guardamos en session_state para el tutor de IA
            st.session_state['df_mat'] = df_mat
            st.session_state['df_en'] = df_en
            
        except Exception as e:
            st.error(f"Error en la simulación: No se logró la convergencia. Detalle: {e}")

# ================= TUTOR IA DE INGENIERÍA QUÍMICA =================
st.divider()
st.header("🤖 Tutor de Ingeniería (Gemini)")

if 'df_mat' in st.session_state and 'df_en' in st.session_state:
    if st.button("Analizar resultados con IA"):
        with st.spinner("El tutor IA está analizando los balances..."):
            try:
                # 1. Configurar la API Key desde los "Secrets" de Streamlit
                genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                modelo = genai.GenerativeModel('gemini-2.5-pro')
                
                # 2. Convertir las tablas a texto entendible para la IA
                tabla_materia_md = st.session_state['df_mat'].to_markdown()
                tabla_energia_md = st.session_state['df_en'].to_markdown()
                
                # 3. Prompt de Ingeniería (Contexto)
                prompt = f"""
                Actúa como un profesor experto en ingeniería química. Aquí tienes los resultados 
                de una simulación en BioSTEAM de un proceso de separación de etanol y agua.
                
                BALANCE DE MATERIA:
                {tabla_materia_md}
                
                BALANCE DE ENERGÍA:
                {tabla_energia_md}
                
                Como tutor, por favor:
                1. Analiza brevemente si la separación en el tanque Flash fue eficiente basándote en la concentración de etanol en el 'Vapor caliente'.
                2. Identifica cuál es el equipo que consume más energía y por qué.
                3. Da una recomendación técnica para optimizar el consumo de energía del proceso.
                Mantén un tono académico, claro y motivador.
                """
                
                # 4. Llamada a la API y renderizado
                respuesta = modelo.generate_content(prompt)
                st.markdown(respuesta.text)
            except KeyError:
                st.error("⚠️ Falta la clave de API. Asegúrate de configurar GEMINI_API_KEY en los 'Secrets' de Streamlit Cloud.")
            except Exception as e:
                st.error(f"Error de conexión con la IA: {e}")
else:
    st.info("Ejecuta la simulación primero con el botón de la barra lateral para que el tutor pueda analizar los datos.")
