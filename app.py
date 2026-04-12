import streamlit as st
import biosteam as bst
import thermosteam as tmo
import pandas as pd
import google.generativeai as genai
import os

# ================= CONFIGURACIÓN DE PÁGINA =================
st.set_page_config(page_title="Simulador y TEA de Etanol", layout="wide")

# ================= CLASE ECONÓMICA =================
class TEA_Didactico(bst.TEA):
    def _DPI(self, installed_equipment_cost):
        return self.purchase_cost

    def _TDC(self, DPI):
        return DPI

    def _FCI(self, TDC):
        return self.purchase_cost * self.lang_factor

    def _TCI(self, FCI):
        return FCI + self.WC

    def _FOC(self, FCI):
        return 0.0

    @property
    def VOC(self):
        mat = getattr(self.system, "material_cost", 0)
        util = getattr(self.system, "utility_cost", 0)
        return mat + util

# ================= FUNCIONES DE SIMULACIÓN =================
def generar_reporte(sistema):
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

    datos_en = []
    for u in sistema.units:
        calor_kw = 0.0
        tipo_servicio = "-"
        try:
            if isinstance(u, bst.HXprocess):
                calor_kw = (u.outs[0].H - u.ins[0].H) / 3600
                tipo_servicio = "Recuperacion Interna"
            elif isinstance(u, bst.Flash):
                calor_kw = getattr(u, 'Hnet', 0) / 3600
                tipo_servicio = "Separación Flash"
            elif hasattr(u, "duty") and u.duty is not None:
                calor_kw = u.duty / 3600
                if calor_kw > 0.01: tipo_servicio = "Calentamiento (Vapor)"
                if calor_kw < -0.01: tipo_servicio = "Enfriamiento (Agua)"
        except Exception:
            calor_kw = 0.0

        potencia = getattr(u.power_utility, 'rate', 0.0) if hasattr(u, "power_utility") and u.power_utility else 0.0

        if abs(calor_kw) > 0.01:
            datos_en.append({"ID Equipo": u.ID, "Función": tipo_servicio, "Energia Térmica (kw)": f"{calor_kw:.2f}"})
        if potencia > 0.01:
            datos_en.append({"ID Equipo": u.ID, "Función": "Motor bomba", "Energia electrica (kw)": f"{potencia:.2f}"})

    df_en = pd.DataFrame(datos_en).set_index("ID Equipo")
    return df_mat, df_en

def ejecutar_simulacion(agua_feed, etanol_feed, temp_feed, temp_w220, presion_v100_pa, 
                        precio_luz, precio_vapor, precio_agua, precio_mosto, precio_etanol):
    
    bst.main_flowsheet.clear()
    
    chemicals = tmo.Chemicals(["Water", "Ethanol"])
    bst.settings.set_thermo(chemicals)
    
    mosto = bst.Stream("1-MOSTO", Water=agua_feed, Ethanol=etanol_feed, units="kg/h", T=temp_feed + 273.15, P=101325)
    vinazas_retorno = bst.Stream("Vinazas-Retorno", Water=200, Ethanol=0, units="kg/h", T=95 + 273.15, P=300000)
    
    P100 = bst.Pump("P-100", ins=mosto, P=4*101325)
    W210 = bst.HXprocess("W-210", ins=(P100-0, vinazas_retorno), outs=("3-Mosto-Pre", "Drenaje"), phase0="l", phase1="l")
    W210.outs[0].T = 85 + 273.15
    W220 = bst.HXutility("W-220", ins=W210-0, outs="Mezcla", T=temp_w220 + 273.15)
    V100 = bst.IsenthalpicValve("V-100", ins=W220-0, outs="Mezcla-Bifasica", P=presion_v100_pa)
    V1 = bst.Flash("V-1", ins=V100-0, outs=("Vapor caliente", "Vinazas"), P=presion_v100_pa, Q=0)
    W310 = bst.HXutility("W-310", ins=V1-0, outs="Producto Final", T=25 + 273.15)
    P200 = bst.Pump("P-200", ins=V1-1, outs=vinazas_retorno, P=3*101325)
    
    eth_sys = bst.System("planta_etanol", path=(P100, W210, W220, V100, V1, W310, P200))
    eth_sys.simulate()

    # Configuración de Precios
    bst.PowerUtility.price = precio_luz
    vapor = bst.HeatUtility.get_agent("low_pressure_steam")
    vapor.heat_transfer_price = precio_vapor
    agua = bst.HeatUtility.get_agent("cooling_water")
    agua.heat_transfer_price = precio_agua
    mosto.price = precio_mosto
    
    eth_sys.simulate()

    producto = W310.outs[0]
    flujo_total = producto.F_mass
    
    # Validación física de la corriente
    if flujo_total <= 0.001:
        raise ValueError("Las condiciones actuales (Temperatura/Presión) no evaporan el etanol. El flujo de producto es 0 kg/h, por lo que el análisis económico no se puede calcular. ¡Prueba subiendo la temperatura del W-220 o bajando la presión del V-100!")

    tea = TEA_Didactico(
        system=eth_sys, IRR=0.15, duration=(2025, 2045), income_tax=0.3, depreciation="MACRS7",
        construction_schedule=(0.4, 0.6), startup_months=6, startup_FOCfrac=0.5, startup_VOCfrac=0.5,
        startup_salesfrac=0.5, operating_days=330, lang_factor=4.0, WC_over_FCI=0.05,
        finance_interest=0.0, finance_years=0.0, finance_fraction=0.0,
    )

    comp_etanol = (producto.imass["Ethanol"] / flujo_total * 100)
    datos_producto = {
        "Temp": producto.T - 273.15,
        "Presion": producto.P / 101325,
        "Flujo": flujo_total,
        "Composicion": comp_etanol
    }

    tea.IRR = 0.0
    costo_produccion = tea.solve_price(producto)
    
    tea.IRR = 0.15
    precio_venta = tea.solve_price(producto)
    
    producto.price = precio_etanol
    tea.IRR = 0.15
    
    datos_tea = {
        "Costo_Produccion": costo_produccion,
        "Precio_Sugerido": precio_venta,
        "NPV": tea.NPV,
        "PBP": tea.PBP,
        "ROI": tea.ROI
    }

    df_mat, df_en = generar_reporte(eth_sys)
    diagram_path = "diagrama_etanol_final"
    eth_sys.diagram(file=diagram_path, format="png")
    
    return df_mat, df_en, datos_producto, datos_tea, diagram_path + ".png"


# ================= INTERFAZ STREAMLIT =================
st.title("🏭 Simulador y Análisis TEA: Planta de Etanol")

st.sidebar.header("⚙️ Parámetros de Operación")
temp_feed = st.sidebar.slider("Temperatura Mosto (°C)", min_value=10, max_value=80, value=25)
temp_w220 = st.sidebar.slider("Temperatura Calentador W-220 (°C)", min_value=80, max_value=120, value=92)
presion_v100_atm = st.sidebar.slider("Presión V-100 (atm)", min_value=0.1, max_value=3.0, value=1.0)
presion_v100_pa = presion_v100_atm * 101325 

agua_feed = st.sidebar.number_input("Flujo de Agua (kg/h)", value=900)
etanol_feed = st.sidebar.number_input("Flujo de Etanol (kg/h)", value=100)

st.sidebar.markdown("---")
st.sidebar.header("💰 Parámetros Económicos")
precio_luz = st.sidebar.slider("Precio Luz ($/kWh)", min_value=0.01, max_value=0.20, value=0.085, step=0.005, format="$%.3f")
precio_vapor = st.sidebar.slider("Precio Vapor ($/MJ)", min_value=0.01, max_value=0.10, value=0.025, step=0.005, format="$%.3f")
precio_agua = st.sidebar.slider("Precio Agua ($/MJ)", min_value=0.0001, max_value=0.0050, value=0.0005, step=0.0001, format="$%.4f")
precio_mosto = st.sidebar.slider("Precio Mosto ($/kg)", min_value=0.0000001, max_value=0.0000100, value=0.0000005, step=0.0000001, format="$%.7f")
precio_etanol = st.sidebar.slider("Precio Venta Etanol ($/kg)", min_value=0.5, max_value=3.0, value=1.2, step=0.1, format="$%.2f")

if st.sidebar.button("🚀 Simular Proceso y Economía"):
    with st.spinner("Calculando balances y variables económicas..."):
        try:
            df_mat, df_en, prod_info, tea_info, diagram_file = ejecutar_simulacion(
                agua_feed, etanol_feed, temp_feed, temp_w220, presion_v100_pa,
                precio_luz, precio_vapor, precio_agua, precio_mosto, precio_etanol
            )
            
            st.subheader("📦 Propiedades del Producto Final")
            col_p1, col_p2, col_p3, col_p4 = st.columns(4)
            col_p1.metric("Temperatura", f"{prod_info['Temp']:.1f} °C")
            col_p2.metric("Presión", f"{prod_info['Presion']:.2f} atm")
            col_p3.metric("Flujo Másico", f"{prod_info['Flujo']:.1f} kg/h")
            col_p4.metric("Composición Etanol", f"{prod_info['Composicion']:.1f} %")
            
            st.markdown("---")
            
            st.subheader("💵 Resultados del Análisis Económico (TEA)")
            col_t1, col_t2, col_t3, col_t4, col_t5 = st.columns(5)
            col_t1.metric("Costo Real Prod.", f"${tea_info['Costo_Produccion']:.2f} /kg")
            col_t2.metric("Precio Sugerido", f"${tea_info['Precio_Sugerido']:.2f} /kg")
            col_t3.metric("NPV", f"${tea_info['NPV']:,.0f}")
            col_t4.metric("Payback (PBP)", f"{tea_info['PBP']:.1f} años")
            col_t5.metric("ROI", f"{tea_info['ROI']:.1f} %")
            
            st.markdown("---")
            
            col_m1, col_m2 = st.columns(2)
            with col_m1:
                st.subheader("Balance de Materia")
                st.dataframe(df_mat, use_container_width=True)
            with col_m2:
                st.subheader("Balance de Energía")
                st.dataframe(df_en, use_container_width=True)
                
            st.subheader("🗺️ Diagrama de Flujo (PFD BioSTEAM)")
            if os.path.exists(diagram_file):
                st.image(diagram_file)
            else:
                st.warning("El diagrama no se pudo renderizar. Verifica que graphviz esté instalado en el sistema.")

       # ================= NUEVA SECCIÓN: PLANOS ISO (AUTOCAD) =================
            st.markdown("---")
            st.subheader("📐 Planos de Ingeniería (Normas ISO)")
            st.write("Documentación técnica generada en AutoCAD Plant 3D:")
            st.write("") # Espacio en blanco para que respire el diseño
            
            col_pdf1, col_pdf2 = st.columns(2)
            
            with col_pdf1:
                # Texto descriptivo explícito sobre el archivo 1
                st.markdown("**1. Diagrama de Bloques**")
                st.caption("Representación general de las etapas del proceso bajo normativas ISO.")
                
                if os.path.exists("diagrama_bloques.pdf"):
                    with open("diagrama_bloques.pdf", "rb") as file:
                        st.download_button(
                            label="📥 Descargar Diagrama de Bloques",
                            data=file,
                            file_name="Diagrama_Bloques_ISO.pdf",
                            mime="application/pdf"
                        )
                else:
                    st.info("Aún no se ha subido el archivo 'diagrama_bloques.pdf' al repositorio.")
            
            with col_pdf2:
                # Texto descriptivo explícito sobre el archivo 2
                st.markdown("**2. Diagrama de Flujo de Proceso (PFD)**")
                st.caption("Avance del diagrama detallado con instrumentación bajo normativas ISO.")
                
                if os.path.exists("diagrama_flujo.pdf"):
                    with open("diagrama_flujo.pdf", "rb") as file:
                        st.download_button(
                            label="📥 Descargar PFD (AutoCAD)",
                            data=file,
                            file_name="Diagrama_Flujo_Proceso_ISO.pdf",
                            mime="application/pdf"
                        )
                else:
                    st.info("Aún no se ha subido el archivo 'diagrama_flujo.pdf' al repositorio.")
            # =======================================================================
            
            st.session_state['df_mat'] = df_mat
            st.session_state['df_en'] = df_en
            
        except ValueError as ve:
            st.warning(ve)
        except Exception as e:
            st.error(f"Error en la simulación: {e}")

# ================= TUTOR IA DE INGENIERÍA QUÍMICA =================
st.divider()
st.header("🤖 Tutor de Ingeniería (Gemini)")

if 'df_mat' in st.session_state and 'df_en' in st.session_state:
    if st.button("Analizar resultados con IA"):
        with st.spinner("El tutor IA está analizando los balances..."):
            try:
                genai.configure(api_key=st.secrets["GEMINI_API_KEY"])
                modelo = genai.GenerativeModel('gemini-1.5-flash')
                
                prompt = f"""
                Actúa como un profesor experto en ingeniería química. Aquí tienes los resultados 
                de una simulación en BioSTEAM.
                
                BALANCE DE MATERIA:
                {st.session_state['df_mat'].to_markdown()}
                
                BALANCE DE ENERGÍA:
                {st.session_state['df_en'].to_markdown()}
                
                Como tutor, por favor:
                1. Analiza brevemente la separación en el tanque Flash.
                2. Identifica cuál es el equipo que consume más energía y por qué.
                3. Da una recomendación técnica para optimizar el proceso.
                Mantén un tono académico y claro.
                """
                
                respuesta = modelo.generate_content(prompt)
                st.markdown(respuesta.text)
            except KeyError:
                st.error("⚠️ Falta la clave de API (GEMINI_API_KEY) en los 'Secrets'.")
            except Exception as e:
                st.error(f"Error de conexión con la IA: {e}")
else:
    st.info("Ejecuta la simulación primero para que el tutor pueda analizar los datos.")
