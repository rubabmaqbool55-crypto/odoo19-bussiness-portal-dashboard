/** @odoo-module **/

import { loadJS } from "@web/core/assets";
import { rpc } from "@web/core/network/rpc";

const CHART_ROUTE = "/my/business-dashboard/charts/data";
const CHART_LIBRARY_URL = "/web/static/lib/Chart/Chart.js";
let chartLibraryPromise;

async function loadChartLibrary() {
    if (window.Chart) {
        return window.Chart;
    }
    if (!chartLibraryPromise) {
        chartLibraryPromise = loadJS(CHART_LIBRARY_URL).then(() => {
            if (!window.Chart) {
                throw new Error("Chart.js loaded but did not create window.Chart");
            }
            return window.Chart;
        });
    }
    return chartLibraryPromise;
}

function numberLocale() {
    return (document.documentElement.lang || "en-US").replace("_", "-");
}

function formatMoney(value, currency, compact = false) {
    const amount = Number(value || 0);
    try {
        return new Intl.NumberFormat(numberLocale(), {
            style: "currency",
            currency: currency.code,
            notation: compact ? "compact" : "standard",
            maximumFractionDigits: compact ? 1 : currency.decimal_places,
            minimumFractionDigits: compact ? 0 : currency.decimal_places,
        }).format(amount);
    } catch {
        const formatted = new Intl.NumberFormat(numberLocale(), {
            notation: compact ? "compact" : "standard",
            maximumFractionDigits: compact ? 1 : currency.decimal_places,
        }).format(amount);
        return currency.position === "after"
            ? `${formatted} ${currency.symbol}`
            : `${currency.symbol} ${formatted}`;
    }
}

function formatInteger(value) {
    return new Intl.NumberFormat(numberLocale(), {
        maximumFractionDigits: 0,
    }).format(Number(value || 0));
}

function cssValue(root, name, fallback) {
    return getComputedStyle(root).getPropertyValue(name).trim() || fallback;
}

function palette(root) {
    return {
        primary: cssValue(root, "--bpd-chart-primary", "#2563eb"),
        secondary: cssValue(root, "--bpd-chart-secondary", "#7c3aed"),
        success: cssValue(root, "--bpd-chart-success", "#059669"),
        warning: cssValue(root, "--bpd-chart-warning", "#d97706"),
        danger: cssValue(root, "--bpd-chart-danger", "#dc2626"),
        info: cssValue(root, "--bpd-chart-info", "#0891b2"),
        muted: cssValue(root, "--bpd-chart-muted", "#64748b"),
        grid: cssValue(root, "--bpd-chart-grid", "rgba(148, 163, 184, 0.22)"),
        text: cssValue(root, "--bpd-chart-text", "#475569"),
    };
}

function hasValues(values) {
    return values.some((value) => Number(value || 0) !== 0);
}

function setCardState(root, key, visible, hasData) {
    const card = root.querySelector(`[data-chart-card="${key}"]`);
    const canvas = root.querySelector(`[data-chart="${key}"]`);
    const empty = root.querySelector(`[data-chart-empty="${key}"]`);
    if (!card) {
        return false;
    }
    card.hidden = !visible;
    if (!visible) {
        return false;
    }
    canvas.hidden = !hasData;
    empty.hidden = hasData;
    return true;
}

function setMeta(root, key, text) {
    const element = root.querySelector(`[data-chart-meta="${key}"]`);
    if (element) {
        element.textContent = text;
    }
}

function openChartUrl(event, elements, urls, mode = "item") {
    if (!elements.length || !urls) {
        return;
    }
    const point = elements[0];
    const index = mode === "dataset" ? point.datasetIndex : point.index;
    const url = Array.isArray(urls) ? urls[index] : urls;
    if (url) {
        window.location.assign(url);
    }
}

function commonOptions(colors, currency = null) {
    return {
        responsive: true,
        maintainAspectRatio: false,
        interaction: {
            mode: "nearest",
            intersect: true,
        },
        animation: {
            duration: 550,
        },
        plugins: {
            legend: {
                position: "bottom",
                labels: {
                    color: colors.text,
                    boxWidth: 12,
                    boxHeight: 12,
                    usePointStyle: true,
                    padding: 18,
                },
            },
            tooltip: {
                padding: 12,
                displayColors: true,
                callbacks: currency
                    ? {
                          label(context) {
                              const label = context.dataset.label
                                  ? `${context.dataset.label}: `
                                  : "";
                              return `${label}${formatMoney(context.raw, currency)}`;
                          },
                      }
                    : {},
            },
        },
    };
}

function renderSummary(root, summary, currency) {
    for (const [key, item] of Object.entries(summary)) {
        const label = root.querySelector(`[data-summary-label="${key}"]`);
        const value = root.querySelector(`[data-summary-value="${key}"]`);
        const note = root.querySelector(`[data-summary-note="${key}"]`);
        if (label) {
            label.textContent = item.label;
        }
        if (value) {
            value.textContent = formatMoney(item.value, currency);
        }
        if (note) {
            note.textContent = item.count
                ? `${formatInteger(item.count)} document(s)`
                : "";
        }
    }
}

function renderTrend(root, chartData, currency, colors, ChartClass) {
    const data = chartData.charts.trend;
    const allValues = data.series.flatMap((series) => series.values);
    if (!setCardState(root, "trend", data.visible, hasValues(allValues))) {
        return;
    }

    const canvas = root.querySelector('[data-chart="trend"]');
    const seriesColors = [colors.primary, colors.secondary, colors.success];
    const options = commonOptions(colors, currency);
    options.onClick = (event, elements) =>
        openChartUrl(
            event,
            elements,
            data.series.map((series) => series.url),
            "dataset"
        );
    options.scales = {
        x: {
            grid: { display: false },
            ticks: { color: colors.text, maxRotation: 0, autoSkip: true },
        },
        y: {
            beginAtZero: true,
            grid: { color: colors.grid },
            ticks: {
                color: colors.text,
                callback(value) {
                    return formatMoney(value, currency, true);
                },
            },
        },
    };

    new ChartClass(canvas, {
        type: "line",
        data: {
            labels: data.labels,
            datasets: data.series.map((series, index) => ({
                label: series.label,
                data: series.values,
                borderColor: seriesColors[index],
                backgroundColor: seriesColors[index],
                pointBackgroundColor: seriesColors[index],
                pointBorderColor: "#ffffff",
                pointBorderWidth: 2,
                pointRadius: 3,
                pointHoverRadius: 5,
                borderWidth: 2.5,
                tension: 0.32,
                fill: false,
            })),
        },
        options,
    });
}

function renderInvoiceStatus(root, chartData, colors, ChartClass) {
    const data = chartData.charts.invoice_status;
    const values = data.items.map((item) => item.value);
    if (!setCardState(root, "invoice_status", data.visible, hasValues(values))) {
        return;
    }
    const title = root.querySelector('[data-chart-title="invoice_status"]');
    if (title && data.title) {
        title.textContent = data.title;
    }
    setMeta(
        root,
        "invoice_status",
        `${formatInteger(values.reduce((total, value) => total + value, 0))} document(s)`
    );

    const canvas = root.querySelector('[data-chart="invoice_status"]');
    const options = commonOptions(colors);
    options.cutout = "68%";
    options.onClick = (event, elements) =>
        openChartUrl(
            event,
            elements,
            data.items.map((item) => item.url)
        );
    options.plugins.tooltip.callbacks = {
        label(context) {
            return `${context.label}: ${formatInteger(context.raw)}`;
        },
    };

    new ChartClass(canvas, {
        type: "doughnut",
        data: {
            labels: data.items.map((item) => item.label),
            datasets: [
                {
                    data: values,
                    backgroundColor: [
                        colors.success,
                        colors.info,
                        colors.warning,
                        colors.danger,
                    ],
                    borderWidth: 0,
                    hoverOffset: 7,
                },
            ],
        },
        options,
    });
}

function renderAging(root, chartData, currency, colors, ChartClass) {
    const data = chartData.charts.aging;
    const values = data.items.map((item) => item.value);
    if (!setCardState(root, "aging", data.visible, hasValues(values))) {
        return;
    }
    setMeta(root, "aging", `Total ${formatMoney(data.total, currency)}`);
    const canvas = root.querySelector('[data-chart="aging"]');
    const options = commonOptions(colors, currency);
    options.indexAxis = "y";
    options.onClick = (event, elements) =>
        openChartUrl(
            event,
            elements,
            data.items.map((item) => item.url)
        );
    options.plugins.legend.display = false;
    options.scales = {
        x: {
            beginAtZero: true,
            grid: { color: colors.grid },
            ticks: {
                color: colors.text,
                callback(value) {
                    return formatMoney(value, currency, true);
                },
            },
        },
        y: {
            grid: { display: false },
            ticks: { color: colors.text },
        },
    };

    new ChartClass(canvas, {
        type: "bar",
        data: {
            labels: data.items.map((item) => item.label),
            datasets: [
                {
                    label: "Outstanding",
                    data: values,
                    backgroundColor: [
                        colors.info,
                        colors.warning,
                        colors.warning,
                        colors.danger,
                        colors.danger,
                    ],
                    borderRadius: 7,
                    borderSkipped: false,
                    maxBarThickness: 28,
                },
            ],
        },
        options,
    });
}

function renderDelivery(root, chartData, colors, ChartClass) {
    const data = chartData.charts.delivery;
    const allValues = data.series.flatMap((series) => series.values);
    if (!setCardState(root, "delivery", data.visible, hasValues(allValues))) {
        return;
    }
    setMeta(root, "delivery", `${formatInteger(data.on_time_rate)}% on-time rate`);
    const canvas = root.querySelector('[data-chart="delivery"]');
    const options = commonOptions(colors);
    options.onClick = (event, elements) => openChartUrl(event, elements, data.url);
    options.plugins.tooltip.callbacks = {
        label(context) {
            return `${context.dataset.label}: ${formatInteger(context.raw)}`;
        },
    };
    options.scales = {
        x: {
            stacked: true,
            grid: { display: false },
            ticks: { color: colors.text, maxRotation: 0, autoSkip: true },
        },
        y: {
            stacked: true,
            beginAtZero: true,
            grid: { color: colors.grid },
            ticks: { color: colors.text, precision: 0 },
        },
    };

    new ChartClass(canvas, {
        type: "bar",
        data: {
            labels: data.labels,
            datasets: [
                {
                    label: data.series[0].label,
                    data: data.series[0].values,
                    backgroundColor: colors.success,
                    borderRadius: 5,
                    borderSkipped: false,
                },
                {
                    label: data.series[1].label,
                    data: data.series[1].values,
                    backgroundColor: colors.danger,
                    borderRadius: 5,
                    borderSkipped: false,
                },
            ],
        },
        options,
    });
}

function renderProjects(root, chartData, colors, ChartClass) {
    const data = chartData.charts.projects;
    const values = data.items.map((item) => item.value);
    if (!setCardState(root, "projects", data.visible, data.items.length > 0)) {
        return;
    }
    setMeta(root, "projects", `${formatInteger(data.items.length)} active project(s)`);
    const canvas = root.querySelector('[data-chart="projects"]');
    const options = commonOptions(colors);
    options.indexAxis = "y";
    options.onClick = (event, elements) =>
        openChartUrl(
            event,
            elements,
            data.items.map((item) => item.url)
        );
    options.plugins.legend.display = false;
    options.plugins.tooltip.callbacks = {
        label(context) {
            const item = data.items[context.dataIndex];
            return `${formatInteger(context.raw)}% — ${item.completed}/${item.total} completed`;
        },
    };
    options.scales = {
        x: {
            beginAtZero: true,
            max: 100,
            grid: { color: colors.grid },
            ticks: {
                color: colors.text,
                callback(value) {
                    return `${value}%`;
                },
            },
        },
        y: {
            grid: { display: false },
            ticks: { color: colors.text },
        },
    };

    new ChartClass(canvas, {
        type: "bar",
        data: {
            labels: data.items.map((item) => item.label),
            datasets: [
                {
                    label: "Progress",
                    data: values,
                    backgroundColor: colors.primary,
                    borderRadius: 7,
                    borderSkipped: false,
                    maxBarThickness: 28,
                },
            ],
        },
        options,
    });
}

function renderTasks(root, chartData, colors, ChartClass) {
    const data = chartData.charts.tasks;
    const values = data.items.map((item) => item.value);
    if (!setCardState(root, "tasks", data.visible, hasValues(values))) {
        return;
    }
    setMeta(
        root,
        "tasks",
        `${formatInteger(values.reduce((total, value) => total + value, 0))} task(s)`
    );
    const canvas = root.querySelector('[data-chart="tasks"]');
    const options = commonOptions(colors);
    options.cutout = "68%";
    options.onClick = (event, elements) =>
        openChartUrl(
            event,
            elements,
            data.items.map((item) => item.url)
        );
    options.plugins.tooltip.callbacks = {
        label(context) {
            return `${context.label}: ${formatInteger(context.raw)}`;
        },
    };

    new ChartClass(canvas, {
        type: "doughnut",
        data: {
            labels: data.items.map((item) => item.label),
            datasets: [
                {
                    data: values,
                    backgroundColor: [
                        colors.info,
                        colors.warning,
                        colors.danger,
                        colors.success,
                    ],
                    borderWidth: 0,
                    hoverOffset: 7,
                },
            ],
        },
        options,
    });
}

function destroyRenderedCharts(root, ChartClass) {
    root
        .querySelectorAll("canvas[data-chart]")
        .forEach((canvas) => {
            const existingChart =
                typeof ChartClass.getChart === "function"
                    ? ChartClass.getChart(canvas)
                    : null;

            if (existingChart) {
                existingChart.destroy();
            }
        });
}


async function loadChartsForPeriod(root, period) {
    const loading = root.querySelector(
        "[data-chart-loading]"
    );
    const error = root.querySelector(
        "[data-chart-error]"
    );
    const errorMessage = root.querySelector(
        "[data-chart-error-message]"
    );
    const warning = root.querySelector(
        "[data-chart-warning]"
    );
    const periodSelect = root.querySelector(
        "[data-chart-period-select]"
    );

    loading.hidden = false;
    error.hidden = true;

    if (warning) {
        warning.hidden = true;
    }

    root.classList.remove("has-error");

    try {
        const [ChartClass, chartData] =
            await Promise.all([
                loadChartLibrary(),
                rpc(CHART_ROUTE, {
                    period,
                }),
            ]);

        destroyRenderedCharts(
            root,
            ChartClass
        );

        root.dataset.period = chartData.period;

        if (periodSelect) {
            periodSelect.value = chartData.period;
        }

        root
            .querySelectorAll(
                "[data-chart-period-label]"
            )
            .forEach((element) => {
                element.textContent =
                    chartData.period_label;
            });

        const colors = palette(root);

        ChartClass.defaults.font.family =
            getComputedStyle(
                document.body
            ).fontFamily;

        ChartClass.defaults.color =
            colors.text;

        renderSummary(
            root,
            chartData.summary,
            chartData.currency
        );

        renderTrend(
            root,
            chartData,
            chartData.currency,
            colors,
            ChartClass
        );

        renderInvoiceStatus(
            root,
            chartData,
            colors,
            ChartClass
        );

        renderAging(
            root,
            chartData,
            chartData.currency,
            colors,
            ChartClass
        );

        renderDelivery(
            root,
            chartData,
            colors,
            ChartClass
        );

        renderProjects(
            root,
            chartData,
            colors,
            ChartClass
        );

        renderTasks(
            root,
            chartData,
            colors,
            ChartClass
        );

        const visibleCards =
            root.querySelectorAll(
                "[data-chart-card]:not([hidden])"
            ).length;

        if (!visibleCards) {
            throw new Error(
                "No chart sections are available"
            );
        }

        loading.hidden = true;
        error.hidden = true;

        if (warning) {
            warning.hidden =
                !chartData.partial_failure;
        }

        root.classList.add("is-ready");
    } catch (chartError) {
        console.error(
            "Business portal charts failed to load",
            chartError
        );

        loading.hidden = true;
        error.hidden = false;

        if (errorMessage) {
            errorMessage.textContent =
                "Dashboard analytics could not be loaded.";
        }

        root.classList.add("has-error");
    }
}


async function initializeCharts(root) {
    if (
        root.dataset.chartsInitialized === "1"
    ) {
        return;
    }

    root.dataset.chartsInitialized = "1";

    const periodSelect = root.querySelector(
        "[data-chart-period-select]"
    );

    const defaultPeriod =
        root.dataset.period || "12m";

    if (periodSelect) {
        periodSelect.value = defaultPeriod;

        periodSelect.addEventListener(
            "change",
            async () => {
                await loadChartsForPeriod(
                    root,
                    periodSelect.value
                );
            }
        );
    }

    await loadChartsForPeriod(
        root,
        defaultPeriod
    );
}

function startCharts() {
    document
        .querySelectorAll(".bpd-charts-root")
        .forEach((root) => initializeCharts(root));
}

if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", startCharts, { once: true });
} else {
    startCharts();
}
