// ClickerBridge — мод-прослойка между внешним кликером и Unity-игрой (BepInEx 6, IL2CPP).
//
// Кликер подключается к 127.0.0.1:<порт> и шлёт текстовые команды, по одной на строку:
//   ping                      -> "pong <ширина> <высота> <focused 0/1>"
//   move <x> <y>              -> переместить «виртуальную мышь»
//   down <кнопка> <x> <y>     -> нажать кнопку (0 = ЛКМ, 1 = ПКМ, 2 = СКМ)
//   up <кнопка> <x> <y>       -> отпустить кнопку
//   click <кнопка> <x> <y>    -> нажать и отпустить (в разных кадрах)
//   release                   -> отпустить всё и вернуть управление настоящей мыши
//   stats                     -> сколько раз игра читала мышь (для диагностики)
// Координаты — пиксели клиентской области окна, от левого верхнего угла (как в кликере).
//
// Пока идут команды, мод подменяет игре UnityEngine.Input (позиция мыши и кнопки) и
// сообщает, что окно активно. Через секунду после последнего отпускания кнопки
// управление возвращается настоящей мыши. Ничего специфичного для конкретной игры
// здесь нет, поэтому мод подходит для любой IL2CPP-игры на старой системе ввода Unity.

using System;
using System.Collections.Concurrent;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using BepInEx;
using BepInEx.Configuration;
using BepInEx.Logging;
using BepInEx.Unity.IL2CPP;
using BepInEx.Unity.IL2CPP.Hook;
using HarmonyLib;
using UnityEngine;

namespace ClickerBridge
{
    [BepInPlugin("clicker.bridge", "ClickerBridge", "1.0.0")]
    public class Plugin : BasePlugin
    {
        internal static ManualLogSource L;

        public override void Load()
        {
            L = Log;
            ConfigEntry<int> port = Config.Bind("Bridge", "Port", 47800, "TCP-порт на 127.0.0.1 для команд кликера");
            ConfigEntry<bool> runInBg = Config.Bind("Bridge", "RunInBackground", true,
                "Не ставить игру на паузу, когда окно не активно");
            Fake.RunInBackground = runInBg.Value;

            var harmony = new Harmony("clicker.bridge");
            foreach (Type t in new[] { typeof(InputPatches), typeof(FocusPatches) })
            {
                try { harmony.PatchAll(t); }
                catch (Exception e) { L.LogError($"Не удалось применить {t.Name}: {e}"); }
            }
            FocusPatches.PatchEventSystems(harmony);
            NativeMouse.Install();

            AddComponent<BridgeBehaviour>();
            Server.Start(port.Value);
        }
    }

    // ---------------- Состояние «виртуальной мыши» ----------------
    internal static class Fake
    {
        public static bool RunInBackground = true;
        public static volatile bool Active;
        public static Vector3 Pos;
        public static readonly bool[] Cur = new bool[3];
        public static readonly bool[] Prev = new bool[3];
        public static float LastActivity;
        public static readonly ConcurrentQueue<string> Commands = new();

        // Кэш для потока сервера (Unity API нельзя трогать вне главного потока)
        public static volatile int ScreenW, ScreenH;
        public static volatile bool Focused;

        // Диагностика: сколько раз игра обращалась к вводу
        public static long NPos, NButton, NDown, NUp, NTouch, NAnyKey;

        public static bool Down(int b) => b >= 0 && b < 3 && Cur[b] && !Prev[b];
        public static bool Up(int b) => b >= 0 && b < 3 && !Cur[b] && Prev[b];
        public static bool Held(int b) => b >= 0 && b < 3 && Cur[b];
        public static bool AnyDown() => Down(0) || Down(1) || Down(2);
    }

    // ---------------- Каждый кадр: применяем команды ----------------
    public class BridgeBehaviour : MonoBehaviour
    {
        public BridgeBehaviour(IntPtr ptr) : base(ptr) { }

        private float _nextStats;

        private void Update()
        {
            if (Fake.RunInBackground && !Application.runInBackground)
                Application.runInBackground = true;

            Fake.ScreenW = Screen.width;
            Fake.ScreenH = Screen.height;
            Fake.Focused = Application.isFocused;

            for (int i = 0; i < 3; i++)
                Fake.Prev[i] = Fake.Cur[i];

            // Не больше одного изменения кнопок за кадр, чтобы игра увидела и нажатие, и отпускание
            bool buttonChanged = false;
            while (Fake.Commands.TryPeek(out string cmd))
            {
                bool isButton = !cmd.StartsWith("move", StringComparison.Ordinal);
                if (isButton && buttonChanged)
                    break;
                Fake.Commands.TryDequeue(out _);
                buttonChanged |= Apply(cmd);
            }

            if (Fake.Active && !Fake.Cur[0] && !Fake.Cur[1] && !Fake.Cur[2]
                && Time.unscaledTime - Fake.LastActivity > 1f)
                Fake.Active = false;

            if (Time.unscaledTime > _nextStats)
            {
                _nextStats = Time.unscaledTime + 30f;
                Plugin.L.LogInfo($"Игра читала: mousePosition={Fake.NPos}, GetMouseButton={Fake.NButton}, " +
                                 $"Down={Fake.NDown}, Up={Fake.NUp}, touch={Fake.NTouch}, anyKey={Fake.NAnyKey}");
            }
        }

        private static bool Apply(string cmd)
        {
            string[] p = cmd.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            try
            {
                switch (p[0])
                {
                    case "move":
                        SetPos(p[1], p[2]);
                        return false;
                    case "down":
                    case "up":
                        int b = int.Parse(p[1]);
                        if (b < 0 || b > 2) return false;
                        if (p.Length >= 4) SetPos(p[2], p[3]);
                        Fake.Cur[b] = p[0] == "down";
                        Fake.Active = true;
                        Fake.LastActivity = Time.unscaledTime;
                        return true;
                    case "release":
                        for (int i = 0; i < 3; i++) Fake.Cur[i] = false;
                        Fake.LastActivity = -10f;
                        return true;
                }
            }
            catch (Exception e)
            {
                Plugin.L.LogWarning($"Плохая команда «{cmd}»: {e.Message}");
            }
            return false;
        }

        private static void SetPos(string xs, string ys)
        {
            float x = float.Parse(xs, System.Globalization.CultureInfo.InvariantCulture);
            float y = float.Parse(ys, System.Globalization.CultureInfo.InvariantCulture);
            Fake.Pos = new Vector3(x, Screen.height - 1 - y, 0f);   // у Unity Y растёт снизу вверх
            Fake.Active = true;
            Fake.LastActivity = Time.unscaledTime;
        }
    }

    // ---------------- Подмена UnityEngine.Input ----------------
    [HarmonyPatch]
    internal static class InputPatches
    {
        [HarmonyPatch(typeof(Input), nameof(Input.mousePosition), MethodType.Getter), HarmonyPrefix]
        private static bool MousePosition(ref Vector3 __result)
        {
            Interlocked.Increment(ref Fake.NPos);
            if (!Fake.Active) return true;
            __result = Fake.Pos;
            return false;
        }

        [HarmonyPatch(typeof(Input), nameof(Input.GetMouseButton)), HarmonyPrefix]
        private static bool GetMouseButton(int button, ref bool __result)
        {
            Interlocked.Increment(ref Fake.NButton);
            if (!Fake.Active) return true;
            __result = Fake.Held(button);
            return false;
        }

        [HarmonyPatch(typeof(Input), nameof(Input.GetMouseButtonDown)), HarmonyPrefix]
        private static bool GetMouseButtonDown(int button, ref bool __result)
        {
            Interlocked.Increment(ref Fake.NDown);
            if (!Fake.Active) return true;
            __result = Fake.Down(button);
            return false;
        }

        [HarmonyPatch(typeof(Input), nameof(Input.GetMouseButtonUp)), HarmonyPrefix]
        private static bool GetMouseButtonUp(int button, ref bool __result)
        {
            Interlocked.Increment(ref Fake.NUp);
            if (!Fake.Active) return true;
            __result = Fake.Up(button);
            return false;
        }

        [HarmonyPatch(typeof(Input), nameof(Input.touchCount), MethodType.Getter), HarmonyPostfix]
        private static void TouchCount() => Interlocked.Increment(ref Fake.NTouch);

        [HarmonyPatch(typeof(Input), nameof(Input.anyKeyDown), MethodType.Getter), HarmonyPostfix]
        private static void AnyKeyDown(ref bool __result)
        {
            Interlocked.Increment(ref Fake.NAnyKey);
            if (Fake.Active && Fake.AnyDown()) __result = true;
        }

        [HarmonyPatch(typeof(Input), nameof(Input.anyKey), MethodType.Getter), HarmonyPostfix]
        private static void AnyKey(ref bool __result)
        {
            Interlocked.Increment(ref Fake.NAnyKey);
            if (Fake.Active && (Fake.Held(0) || Fake.Held(1) || Fake.Held(2))) __result = true;
        }
    }

    // ---------------- Позиция мыши на уровне движка ----------------
    // IL2CPP часто встраивает геттер Input.mousePosition прямо в код игры, и патч Harmony
    // его не видит. Поэтому перехватываем внутреннюю функцию Unity, через которую
    // проходят все чтения позиции: UnityEngine.Input::get_mousePosition_Injected.
    internal static class NativeMouse
    {
        [UnmanagedFunctionPointer(CallingConvention.Cdecl)]
        private delegate void GetPosDelegate(IntPtr ret);

        private static GetPosDelegate _hook;      // держим ссылку, чтобы делегат не собрал GC
        private static GetPosDelegate _original;
        private static INativeDetour _detour;
        public static long Calls;

        public static void Install()
        {
            IntPtr fn = IntPtr.Zero;
            foreach (string name in new[]
                     {
                         "UnityEngine.Input::get_mousePosition_Injected(UnityEngine.Vector3&)",
                         "UnityEngine.Input::get_mousePosition_Injected",
                     })
            {
                fn = Il2CppInterop.Runtime.IL2CPP.il2cpp_resolve_icall(name);
                if (fn != IntPtr.Zero) break;
            }
            if (fn == IntPtr.Zero)
            {
                Plugin.L.LogWarning("Внутренняя функция позиции мыши не найдена, остаётся только патч Harmony");
                return;
            }
            try
            {
                _hook = Hook;
                _detour = INativeDetour.CreateAndApply(fn, _hook, out _original);
                Plugin.L.LogInfo("Позиция мыши перехвачена на уровне движка");
            }
            catch (Exception e)
            {
                Plugin.L.LogError($"Не удалось перехватить позицию мыши: {e}");
            }
        }

        private static unsafe void Hook(IntPtr ret)
        {
            Interlocked.Increment(ref Calls);
            if (Fake.Active && ret != IntPtr.Zero)
            {
                *(Vector3*)ret = Fake.Pos;
                return;
            }
            _original(ret);
        }
    }

    // ---------------- «Окно активно» ----------------
    [HarmonyPatch]
    internal static class FocusPatches
    {
        // EventSystem (кнопки UGUI) перестаёт обрабатывать клики, когда окно теряет фокус.
        // Ищем его по имени: в разных играх UGUI лежит в разных сборках (или его нет вовсе).
        public static void PatchEventSystems(Harmony harmony)
        {
            var prefix = new HarmonyMethod(typeof(FocusPatches).GetMethod(nameof(KeepFocus),
                System.Reflection.BindingFlags.NonPublic | System.Reflection.BindingFlags.Static));
            foreach (var asm in AppDomain.CurrentDomain.GetAssemblies())
            {
                Type t;
                try { t = asm.GetType("UnityEngine.EventSystems.EventSystem"); }
                catch { continue; }
                var m = t?.GetMethod("OnApplicationFocus", new[] { typeof(bool) });
                if (m == null) continue;
                try
                {
                    harmony.Patch(m, prefix: prefix);
                    Plugin.L.LogInfo($"EventSystem найден в {asm.GetName().Name}, фокус зафиксирован");
                }
                catch (Exception e) { Plugin.L.LogWarning($"EventSystem в {asm.GetName().Name}: {e.Message}"); }
            }
        }

        private static void KeepFocus(ref bool hasFocus) => hasFocus = true;

        [HarmonyPatch(typeof(Application), nameof(Application.isFocused), MethodType.Getter), HarmonyPostfix]
        private static void IsFocused(ref bool __result)
        {
            if (Fake.Active) __result = true;
        }
    }

    // ---------------- TCP-сервер для кликера ----------------
    internal static class Server
    {
        public static void Start(int port)
        {
            var thread = new Thread(() => Listen(port)) { IsBackground = true, Name = "ClickerBridge" };
            thread.Start();
        }

        private static void Listen(int port)
        {
            TcpListener listener;
            try
            {
                listener = new TcpListener(IPAddress.Loopback, port);
                listener.Start();
                Plugin.L.LogInfo($"Жду кликер на 127.0.0.1:{port}");
            }
            catch (Exception e)
            {
                Plugin.L.LogError($"Не удалось открыть порт {port}: {e.Message}");
                return;
            }

            while (true)
            {
                try
                {
                    TcpClient client = listener.AcceptTcpClient();
                    new Thread(() => Serve(client)) { IsBackground = true }.Start();
                }
                catch (Exception e)
                {
                    Plugin.L.LogWarning($"Ошибка подключения: {e.Message}");
                }
            }
        }

        private static void Serve(TcpClient client)
        {
            Plugin.L.LogInfo("Кликер подключился");
            try
            {
                using (client)
                using (NetworkStream stream = client.GetStream())
                using (var reader = new StreamReader(stream, Encoding.UTF8))
                using (var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" })
                {
                    string line;
                    while ((line = reader.ReadLine()) != null)
                    {
                        line = line.Trim().ToLowerInvariant();
                        if (line.Length == 0) continue;
                        writer.WriteLine(Handle(line));
                    }
                }
            }
            catch (IOException) { }
            catch (Exception e)
            {
                Plugin.L.LogWarning($"Ошибка соединения: {e.Message}");
            }
            Plugin.L.LogInfo("Кликер отключился");
        }

        private static string Handle(string line)
        {
            string[] p = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
            switch (p[0])
            {
                case "ping":
                    return $"pong {Fake.ScreenW} {Fake.ScreenH} {(Fake.Focused ? 1 : 0)}";
                case "stats":
                    return $"stats native_pos={NativeMouse.Calls} pos={Fake.NPos} button={Fake.NButton} down={Fake.NDown} up={Fake.NUp} " +
                           $"touch={Fake.NTouch} anykey={Fake.NAnyKey}";
                case "click" when p.Length >= 4:
                    Fake.Commands.Enqueue($"down {p[1]} {p[2]} {p[3]}");
                    Fake.Commands.Enqueue($"up {p[1]} {p[2]} {p[3]}");
                    return "ok";
                case "move" when p.Length >= 3:
                case "down" when p.Length >= 2:
                case "up" when p.Length >= 2:
                case "release":
                    Fake.Commands.Enqueue(line);
                    return "ok";
                default:
                    return "error unknown command";
            }
        }
    }
}
