            maybe_update_memory(force=True)
            st.session_state.strengths_data = analyze_strengths()
            save_session_to_google_sheets(final=True)

        st.session_state.app_phase = "show_chart"
        st.rerun()


# ------------------------------
# 階段 5：顯示圖表與下載
# ------------------------------
elif st.session_state.app_phase == "show_chart":
    st.success("🎉 恭喜您完成了一次自我照顧的練習。來看看這次留下的軌跡。")

    col_chart1, col_chart2 = st.columns(2)

    with col_chart1:
        st.markdown("#### 🔋 您的能量流動軌跡")

        if st.session_state.energy_log:
            df_chart = pd.DataFrame(st.session_state.energy_log)

            line = alt.Chart(df_chart).mark_line(color="#424242", size=4).encode(
                x=alt.X(
                    "階段:N",
                    sort=alt.EncodingSortField(field="排序", order="ascending"),
                    title="對話階段",
                    axis=alt.Axis(labelAngle=-45, labelFontSize=12),
                ),
                y=alt.Y("分數:Q", scale=alt.Scale(domain=[0, 10]), title="狀態分數"),
            )

            points = alt.Chart(df_chart).mark_circle(size=150, color="#1E88E5").encode(
                x=alt.X("階段:N", sort=alt.EncodingSortField(field="排序", order="ascending")),
                y=alt.Y("分數:Q"),
                tooltip=["階段", "分數"],
            )

            band_red = alt.Chart(pd.DataFrame({"y1": [7], "y2": [10]})).mark_rect(
                color="#ffcccc", opacity=0.4
            ).encode(y="y1:Q", y2="y2:Q")

            band_green = alt.Chart(pd.DataFrame({"y1": [4], "y2": [7]})).mark_rect(
                color="#ccffcc", opacity=0.4
            ).encode(y="y1:Q", y2="y2:Q")

            band_blue = alt.Chart(pd.DataFrame({"y1": [0], "y2": [4]})).mark_rect(
                color="#cce5ff", opacity=0.4
            ).encode(y="y1:Q", y2="y2:Q")

            first_stage = df_chart["階段"].iloc[0]
            labels = pd.DataFrame({
                "x": [first_stage, first_stage, first_stage],
                "y": [9, 5.5, 2],
                "text": [
                    "紅區：過度激發",
                    "綠區：容納之窗",
                    "藍區：過低激發",
                ],
                "color": ["#d32f2f", "#2e7d32", "#1565c0"],
            })

            text_layer = alt.Chart(labels).mark_text(
                align="left",
                dx=10,
                fontSize=13,
                fontWeight="bold",
                opacity=0.55,
            ).encode(
                x="x:N",
                y="y:Q",
                text="text:N",
                color=alt.Color("color:N", scale=None),
            )

            final_chart = alt.layer(
                band_red,
                band_green,
                band_blue,
                text_layer,
                line,
                points,
            ).properties(height=350)

            st.altair_chart(final_chart, use_container_width=True)
        else:
            st.info("尚無能量紀錄。")

    with col_chart2:
        st.markdown("#### 🌟 您的六大美德優勢 VIA")

        if st.session_state.strengths_data:
            ordered_strengths = {
                key: st.session_state.strengths_data.get(key, 0)
                for key in VIA_KEYS
            }

            df_radar = pd.DataFrame({
                "r": list(ordered_strengths.values()),
                "theta": list(ordered_strengths.keys()),
            })

            fig = px.line_polar(
                df_radar,
                r="r",
                theta="theta",
                line_close=True,
                range_r=[0, 10],
            )

            fig.update_traces(
                fill="toself",
                fillcolor="rgba(255, 165, 0, 0.35)",
                line_color="darkorange",
            )

            fig.update_layout(
                polar=dict(radialaxis=dict(visible=True, range=[0, 10])),
                showlegend=False,
                margin=dict(l=40, r=40, t=20, b=20),
                height=350,
            )

            st.plotly_chart(fig, use_container_width=True)
        else:
            detail = st.session_state.get("strengths_warning", "")
            if detail:
                st.info(f"{detail} 您仍可以下載完整紀錄，能量走勢也已保留。")
            else:
                st.info("這次沒有成功產生 VIA 雷達圖。您仍可以下載完整紀錄，能量走勢也已保留。")

    st.markdown("""
> **教練的悄悄話**  
> 情緒是流動的，而您的力量也不是只在狀態好的時候才存在。  
> 即使在耗能的時刻，您仍可能展現了某些值得被看見的優勢。
""")

    st.markdown("---")

    export_data = build_export_data()
    json_str = json.dumps(export_data, ensure_ascii=False, indent=2)

    col_a, col_b = st.columns(2)

    with col_a:
        st.download_button(
            label="📥 下載專屬充電記憶 JSON",
            data=json_str.encode("utf-8-sig"),
            file_name=f"ChargeCoach_Memory_{sanitize_filename(st.session_state.user_nickname)}_{now_tw().strftime('%Y%m%d')}.json",
            mime="application/json",
            type="primary",
        )

    with col_b:
        if st.button("🏠 登出 / 下一位使用者"):
            reset_app(clear_keys=True)
            st.rerun()
