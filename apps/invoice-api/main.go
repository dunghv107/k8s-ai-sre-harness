package main

import (
	"context"
	"encoding/json"
	"errors"
	"log/slog"
	"math/rand/v2" // nosemgrep: go.lang.security.audit.crypto.math_random.math-random-used
	"net/http"
	"os"
	"os/signal"
	"runtime/debug"
	"strconv"
	"strings"
	"sync/atomic"
	"syscall"
	"time"

	"github.com/exaring/otelpgx"
	"github.com/jackc/pgx/v5/pgxpool"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracehttp"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"

	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
)

const service = "invoice-api"

var (
	pool *pgxpool.Pool
	// shortcut: in-process fault state, per replica only, move to a shared store if replicas > 1
	faultUntil  atomic.Int64
	faultRate   atomic.Uint64
	faultStatus atomic.Int64
)

type traceHandler struct{ slog.Handler }

func (h traceHandler) Handle(ctx context.Context, r slog.Record) error {
	if sc := trace.SpanContextFromContext(ctx); sc.IsValid() {
		r.AddAttrs(slog.String("trace_id", sc.TraceID().String()), slog.String("span_id", sc.SpanID().String()))
	}
	return h.Handler.Handle(ctx, r)
}

func (h traceHandler) WithAttrs(a []slog.Attr) slog.Handler { return traceHandler{h.Handler.WithAttrs(a)} }

func initLogger() {
	base := slog.NewJSONHandler(os.Stdout, &slog.HandlerOptions{ReplaceAttr: func(_ []string, a slog.Attr) slog.Attr {
		switch a.Key {
		case slog.TimeKey:
			a.Key = "ts"
		case slog.LevelKey:
			a.Value = slog.StringValue(strings.ToLower(a.Value.String()))
		}
		return a
	}})
	slog.SetDefault(slog.New(traceHandler{base}).With("service", service))
}

func initTracing(ctx context.Context) func(context.Context) error {
	otel.SetTextMapPropagator(propagation.NewCompositeTextMapPropagator(propagation.TraceContext{}, propagation.Baggage{}))
	exp, err := otlptracehttp.New(ctx)
	if err != nil {
		slog.Error("otel exporter init failed, tracing disabled", "error", err)
		return func(context.Context) error { return nil }
	}
	res, err := resource.New(ctx, resource.WithFromEnv(), resource.WithAttributes())
	if err != nil {
		res = resource.Default()
	}
	tp := sdktrace.NewTracerProvider(sdktrace.WithBatcher(exp), sdktrace.WithResource(res))
	otel.SetTracerProvider(tp)
	return tp.Shutdown
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}

// observe logs each request and marks 5xx server spans as Error.
func observe(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)
		route := strings.TrimPrefix(r.Pattern, r.Method+" ")
		if route == "" {
			route = r.URL.Path
		}
		// otelhttp names the span before routing, so set http.route explicitly
		trace.SpanFromContext(r.Context()).SetAttributes(attribute.String("http.route", route))
		if sw.status >= 500 {
			trace.SpanFromContext(r.Context()).SetStatus(codes.Error, http.StatusText(sw.status))
		}
		slog.InfoContext(r.Context(), "request", "method", r.Method, "route", route, "status", sw.status,
			"duration_ms", float64(time.Since(start).Microseconds())/1000)
	})
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func fail(w http.ResponseWriter, r *http.Request, err error) {
	trace.SpanFromContext(r.Context()).RecordError(err)
	slog.ErrorContext(r.Context(), "unhandled error", "error", err.Error(), "stack", string(debug.Stack()))
	writeJSON(w, http.StatusInternalServerError, map[string]string{"error": "internal error"})
}

func createInvoice(w http.ResponseWriter, r *http.Request) {
	if time.Now().Unix() < faultUntil.Load() && rand.Float64() < float64(faultRate.Load())/1e6 {
		writeJSON(w, int(faultStatus.Load()), map[string]string{"error": "injected fault"})
		return
	}
	var in struct {
		OrderID     int `json:"order_id"`
		AmountCents int `json:"amount_cents"`
	}
	if err := json.NewDecoder(r.Body).Decode(&in); err != nil || in.AmountCents <= 0 {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "amount_cents must be > 0"})
		return
	}
	var id int
	err := pool.QueryRow(r.Context(),
		"INSERT INTO invoice.invoices (order_id, amount_cents) VALUES ($1, $2) RETURNING id", in.OrderID, in.AmountCents).Scan(&id)
	if err != nil {
		fail(w, r, err)
		return
	}
	writeJSON(w, http.StatusCreated, map[string]any{"id": id, "order_id": in.OrderID, "amount_cents": in.AmountCents})
}

func healthz(w http.ResponseWriter, r *http.Request) {
	ctx, cancel := context.WithTimeout(r.Context(), 2*time.Second)
	defer cancel()
	if err := pool.Ping(ctx); err != nil {
		slog.WarnContext(r.Context(), "db unavailable", "error", err.Error())
		writeJSON(w, http.StatusServiceUnavailable, map[string]string{"status": "unavailable"})
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func faultSlow(w http.ResponseWriter, r *http.Request) {
	ms, _ := strconv.Atoi(r.URL.Query().Get("ms"))
	if r.URL.Query().Get("ms") == "" {
		ms = 2000
	}
	ms = min(max(ms, 0), 30000)
	select {
	case <-time.After(time.Duration(ms) * time.Millisecond):
	case <-r.Context().Done():
	}
	writeJSON(w, http.StatusOK, map[string]int{"slept_ms": ms})
}

func faultErrorRate(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	rate, err := strconv.ParseFloat(q.Get("rate"), 64)
	if err != nil {
		rate = 0.3
	}
	status, err := strconv.Atoi(q.Get("status"))
	if err != nil {
		status = 500
	}
	seconds, err := strconv.Atoi(q.Get("seconds"))
	if err != nil {
		seconds = 300
	}
	faultRate.Store(uint64(rate * 1e6))
	faultStatus.Store(int64(status))
	faultUntil.Store(time.Now().Unix() + int64(seconds))
	writeJSON(w, http.StatusOK, map[string]any{"rate": rate, "status": status, "seconds": seconds})
}

func faultCrash(w http.ResponseWriter, _ *http.Request) {
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "exiting"})
	go func() {
		time.Sleep(200 * time.Millisecond)
		os.Exit(1)
	}()
}

func faultOOM(w http.ResponseWriter, _ *http.Request) {
	go func() {
		var hog [][]byte
		for {
			b := make([]byte, 10<<20)
			for i := range b {
				b[i] = 1 // touch pages so they count as resident
			}
			hog = append(hog, b)
			time.Sleep(10 * time.Millisecond)
		}
	}()
	writeJSON(w, http.StatusAccepted, map[string]string{"status": "allocating"})
}

func main() {
	initLogger()
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGTERM, os.Interrupt)
	defer stop()

	shutdownTracing := initTracing(ctx)

	cfg, err := pgxpool.ParseConfig(os.Getenv("DATABASE_URL"))
	if err != nil {
		slog.Error("bad DATABASE_URL", "error", err.Error())
		os.Exit(1)
	}
	cfg.ConnConfig.Tracer = otelpgx.NewTracer()
	cfg.ConnConfig.ConnectTimeout = 3 * time.Second
	// NewWithConfig does not dial, so an unreachable DB never blocks startup
	pool, err = pgxpool.NewWithConfig(ctx, cfg)
	if err != nil {
		slog.Error("pool init failed", "error", err.Error())
		os.Exit(1)
	}

	mux := http.NewServeMux()
	mux.HandleFunc("POST /api/invoices", createInvoice)
	mux.HandleFunc("GET /healthz", healthz)
	mux.HandleFunc("GET /fault/500", func(w http.ResponseWriter, r *http.Request) {
		fail(w, r, errors.New("fault injection: /fault/500"))
	})
	mux.HandleFunc("GET /fault/400", func(w http.ResponseWriter, _ *http.Request) {
		writeJSON(w, http.StatusBadRequest, map[string]string{"error": "fault injection: /fault/400"})
	})
	mux.HandleFunc("GET /fault/slow", faultSlow)
	mux.HandleFunc("POST /fault/error-rate", faultErrorRate)
	mux.HandleFunc("POST /fault/crash", faultCrash)
	mux.HandleFunc("POST /fault/oom", faultOOM)

	port := os.Getenv("PORT")
	if port == "" {
		port = "8080"
	}
	srv := &http.Server{
		Addr:              ":" + port,
		Handler:           otelhttp.NewHandler(observe(mux), service),
		ReadHeaderTimeout: 5 * time.Second,
	}
	go func() {
		<-ctx.Done()
		sctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
		defer cancel()
		srv.Shutdown(sctx)
		shutdownTracing(sctx)
		pool.Close()
	}()
	slog.Info("listening", "port", port)
	if err := srv.ListenAndServe(); err != nil && !errors.Is(err, http.ErrServerClosed) {
		slog.Error("server failed", "error", err.Error())
		os.Exit(1)
	}
}
