import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai

# ==============================================================================
# 1. CONFIGURACIÓN DE LA INTERFAZ DE STREAMLIT Y API DE GEMINI
# ==============================================================================
st.set_page_config(page_title="Simulador BioSTEAM", page_icon="⚗️", layout="wide")
st.title("⚗️ Simulador de Proceso: Recuperación de Etanol")
st.markdown("Ajusta los parámetros en el menú lateral para simular el proceso de destilación y obtener retroalimentación del Tutor IA.")

# Configuración de Gemini desde los Secrets de Streamlit
try:
    genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
except KeyError:
    st.error("⚠️ Falta configurar la GEMINI_API_KEY en los Secrets de Streamlit.")

# Parámetros dinámicos en la barra lateral
st.sidebar.header("⚙️ Parámetros de Operación")
flujo_agua = st.sidebar.slider("Flujo de Agua (kg/h)", min_value=500, max_value=1500, value=900, step=50)
flujo_etanol = st.sidebar.slider("Flujo de Etanol (kg/h)", min_value=50, max_value=300, value=100, step=10)
temp_mosto = st.sidebar.slider("Temperatura del Mosto (°C)", min_value=10, max_value=50, value=25, step=1)

# ==============================================================================
# 2. LÓGICA DE SIMULACIÓN ENCAPSULADA
# ==============================================================================
def ejecutar_simulacion(f_agua, f_etanol, t_mosto):
    # CRÍTICO: Limpiar el flowsheet para evitar errores de "ID duplicado" al mover sliders
    bst.main_flowsheet.clear()
    
    # Definición de compuestos y termodinámica
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)

    # Definición de corrientes con parámetros dinámicos
    mosto = bst.Stream("1-MOSTO",
                       Water=f_agua, Ethanol=f_etanol, units="kg/h",
                       T=t_mosto + 273.15,
                       P=101325)

    vinazas_retorno = bst.Stream("Vinazas-Retorno",
                                 Water=200, Ethanol=0, units="kg/h",
                                 T=95 + 273.15,
                                 P=300000)

    # Selección y conexión de equipos
    P100 = bst.Pump("P-100", ins=mosto, P=4*101325)
    
    W210 = bst.HXprocess("W-210",
                         ins=(P100-0, vinazas_retorno),
                         outs=("3-Mosto-Pre", "Drenaje"),
                         phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15 # Especificación de diseño

    W220 = bst.HXutility("W-220", ins=W210-0, outs="Mezcla", T=92+273.15)
    V100 = bst.IsenthalpicValve("V-100", ins=W220-0, outs="Mezcla-Bifasica", P=101325)
    
    V1 = bst.Flash("V-1", ins=V100-0, outs=("Vapor caliente", "Vinazas"), P=101325, Q=0)
    
    W310 = bst.HXutility("W-310", ins=V1-0, outs="Producto Final", T=25+273.15)
    P200 = bst.Pump("P-200", ins=V1-1, outs=vinazas_retorno, P=3*101325)

    # Agrupar en un sistema y simular
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    eth_sys.simulate()

    # Extraer datos para el Balance de Materia
    datos_mat = []
    for s in eth_sys.streams:
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

    # Extraer datos para el Balance de Energía
    datos_en = []
    for u in eth_sys.units:
        calor_kw = 0.0
        tipo_servicio = "-"
        
        # Recuperación interna
        if isinstance(u, bst.HXprocess):
            calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
            tipo_servicio = "Recuperación Interna"
            
        # Manejo específico para evitar el error de atributo .duty en Flash
        elif isinstance(u, bst.Flash):
            calor_kw = 0.0 
            tipo_servicio = "Adiabático"
            
        # Equipos con servicios auxiliares estándar
        elif hasattr(u, "duty") and u.duty is not None:
            calor_kw = u.duty / 3600
            if calor_kw > 0.01: tipo_servicio = "Calentamiento (Vapor)"
            elif calor_kw < -0.01: tipo_servicio = "Enfriamiento (Agua)"

        # Motores
        potencia = 0.0
        if hasattr(u, "power_utility") and u.power_utility:
            potencia = u.power_utility.rate

        # Guardar en la tabla si hay consumo/generación
        if abs(calor_kw) > 0.01:
            datos_en.append({"ID Equipo": u.ID, "Función": tipo_servicio, "Energía Térmica (kW)": f"{calor_kw:.2f}"})
        if potencia > 0.01:
            datos_en.append({"ID Equipo": u.ID, "Función": "Motor bomba", "Energía Eléctrica (kW)": f"{potencia:.2f}"})

    df_en = pd.DataFrame(datos_en).set_index("ID Equipo")

    # Generar el diagrama PFD para la web
    ruta_diagrama = "diagrama_pfd"
    eth_sys.diagram(file=ruta_diagrama, format="png")
    
    return df_mat, df_en, f"{ruta_diagrama}.png"

# ==============================================================================
# 3. INTERFAZ Y RENDERIZADO WEB
# ==============================================================================
if st.sidebar.button("▶️ Ejecutar Simulación"):
    with st.spinner("Resolviendo balances de materia y energía..."):
        try:
            # Llamamos a la función principal
            df_materia, df_energia, img_path = ejecutar_simulacion(flujo_agua, flujo_etanol, temp_mosto)
            
            # Mostramos las tablas en dos columnas
            col1, col2 = st.columns(2)
            with col1:
                st.subheader("📦 Balance de Materia")
                st.dataframe(df_materia, use_container_width=True)
            with col2:
                st.subheader("⚡ Balance de Energía")
                st.dataframe(df_energia, use_container_width=True)
                
            # Mostramos el diagrama generado
            st.markdown("---")
            st.subheader("🗺️ Diagrama de Flujo del Proceso (PFD)")
            st.image(img_path, use_container_width=True)

            # ==================================================================
            # 4. INTEGRACIÓN CON GEMINI (TUTOR VIRTUAL)
            # ==================================================================
            st.markdown("---")
            st.subheader("🧠 Análisis del Tutor de Ingeniería Química")
            
            # Modelo de IA
            modelo = genai.GenerativeModel('gemini-2.5-pro')
            
            # Prompt dinámico inyectando los DataFrames como texto
            prompt = f"""
            Actúa como un tutor experto en ingeniería química evaluando el desempeño de un estudiante universitario. 
            El estudiante acaba de simular una planta de recuperación de etanol.
            
            Aquí están los resultados de su balance de materia:
            {df_materia.to_markdown()}
            
            Y aquí los del balance de energía:
            {df_energia.to_markdown()}
            
            Escribe un breve análisis (máximo 3 párrafos) evaluando la eficiencia energética 
            de la recuperación de calor interna (W-210) y dando una recomendación práctica para optimizar el reflujo o la 
            temperatura del calentador (W-220) para mejorar la pureza del etanol en la fase de vapor del Flash.
            """
            
            # Llamada a la API y renderizado de la respuesta
            respuesta_ia = modelo.generate_content(prompt)
            st.info(respuesta_ia.text)
            
        except Exception as e:
            st.error(f"Error al ejecutar la simulación: {e}")
            st.warning("Verifica que las librerías Graphviz y BioSTEAM estén correctamente instaladas.")
else:
    st.info("👈 Ajusta los parámetros en el panel izquierdo y haz clic en 'Ejecutar Simulación' para comenzar.")
