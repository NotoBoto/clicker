// Повторяет генерацию interop-сборок BepInEx (Il2CppInteropManager), но перед запуском
// Il2CppInterop удаляет атрибуты Il2CppDummyDll.*, оставшиеся в игре от Il2CppDumper.
// Они дублируют атрибуты Cpp2IL (AddressAttribute и др.), из-за чего генератор падает.

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Security.Cryptography;
using System.Text;
using AsmResolver.DotNet;
using AssetRipper.Primitives;
using Cpp2IL.Core;
using Cpp2IL.Core.Api;
using Cpp2IL.Core.InstructionSets;
using Cpp2IL.Core.OutputFormats;
using Cpp2IL.Core.ProcessingLayers;
using Il2CppInterop.Generator;
using Il2CppInterop.Generator.Runners;
using LibCpp2IL;

internal static class Program
{
    private const string DumperNamespace = "Il2CppDummyDll";

    private static int Main(string[] args)
    {
        if (args.Length < 2)
        {
            Console.WriteLine("Использование: InteropFix <папка игры> <версия Unity, например 2021.3.6f1>");
            return 1;
        }

        string gameDir = args[0];
        string exe = Directory.GetFiles(gameDir, "*.exe").First(f => !f.Contains("UnityCrashHandler"));
        string dataDir = Path.Combine(gameDir, Path.GetFileNameWithoutExtension(exe) + "_Data");
        string gameAssembly = Path.Combine(gameDir, "GameAssembly.dll");
        string metadata = Path.Combine(dataDir, "il2cpp_data", "Metadata", "global-metadata.dat");
        string bepinex = Path.Combine(gameDir, "BepInEx");
        string unityLibs = Path.Combine(bepinex, "unity-libs");
        string interop = Path.Combine(bepinex, "interop");

        Cpp2IL.Core.Logging.Logger.InfoLog += (m, s) => Console.WriteLine($"[Cpp2IL {s}] {m.Trim()}");
        Cpp2IL.Core.Logging.Logger.WarningLog += (m, s) => Console.WriteLine($"[Cpp2IL WARN {s}] {m.Trim()}");
        Cpp2IL.Core.Logging.Logger.ErrorLog += (m, s) => Console.WriteLine($"[Cpp2IL ERR {s}] {m.Trim()}");

        // 1. Cpp2IL: «пустые» сборки из GameAssembly.dll + global-metadata.dat
        InstructionSetRegistry.RegisterInstructionSet<X86InstructionSet>(DefaultInstructionSets.X86_32);
        InstructionSetRegistry.RegisterInstructionSet<X86InstructionSet>(DefaultInstructionSets.X86_64);
        LibCpp2IlBinaryRegistry.RegisterBuiltInBinarySupport();
        Cpp2IlApi.InitializeLibCpp2Il(gameAssembly, metadata, UnityVersion.Parse(args[1]), false);
        var layers = new List<Cpp2IlProcessingLayer> { new AttributeInjectorProcessingLayer() };
        foreach (var l in layers) l.PreProcess(Cpp2IlApi.CurrentAppContext, layers);
        foreach (var l in layers) l.Process(Cpp2IlApi.CurrentAppContext);
        List<AssemblyDefinition> assemblies = new AsmResolverDllOutputFormatDefault().BuildAssemblies(Cpp2IlApi.CurrentAppContext);
        LibCpp2IlMain.Reset();
        Cpp2IlApi.CurrentAppContext = null;

        // 2. Убираем атрибуты Il2CppDumper
        int removed = 0;
        foreach (var asm in assemblies)
        foreach (var module in asm.Modules)
        foreach (var type in module.GetAllTypes())
        {
            removed += Strip(type);
            foreach (var m in type.Methods)
            {
                removed += Strip(m);
                foreach (var p in m.ParameterDefinitions) removed += Strip(p);
            }
            foreach (var f in type.Fields) removed += Strip(f);
            foreach (var p in type.Properties) removed += Strip(p);
            foreach (var e in type.Events) removed += Strip(e);
        }
        Console.WriteLine($"Удалено атрибутов {DumperNamespace}: {removed}");

        // 3. Il2CppInterop: interop-сборки в BepInEx\interop
        Directory.CreateDirectory(interop);
        foreach (string f in Directory.EnumerateFiles(interop, "*.dll")) File.Delete(f);
        var opts = new GeneratorOptions
        {
            GameAssemblyPath = gameAssembly,   // ScanMethodRefs (по умолчанию в 64-битном процессе)
            Source = assemblies,
            OutputDir = interop,
            UnityBaseLibsDir = Directory.Exists(unityLibs) ? unityLibs : null,
        };
        Console.WriteLine("Генерирую interop-сборки…");
        Il2CppInteropGenerator.Create(opts).AddInteropAssemblyGenerator().Run();

        // 4. Хэш, как его считает BepInEx, чтобы он не пытался перегенерировать сам
        File.WriteAllText(Path.Combine(interop, "assembly-hash.txt"), ComputeHash(gameAssembly, unityLibs, bepinex));
        Console.WriteLine($"Готово: {Directory.GetFiles(interop, "*.dll").Length} сборок в {interop}");
        return 0;
    }

    private static int Strip(IHasCustomAttribute member)
    {
        var list = member.CustomAttributes;
        int n = 0;
        for (int i = list.Count - 1; i >= 0; i--)
        {
            if (list[i].Constructor?.DeclaringType?.Namespace?.Value == DumperNamespace)
            {
                list.RemoveAt(i);
                n++;
            }
        }
        return n;
    }

    // Копия Il2CppInteropManager.ComputeHash из BepInEx
    private static string ComputeHash(string gameAssembly, string unityLibs, string bepinex)
    {
        using var md5 = MD5.Create();

        void HashFile(string file)
        {
            using var fs = File.OpenRead(file);
            var buffer = new byte[81920];
            int read;
            while ((read = fs.Read(buffer)) > 0)
                md5.TransformBlock(buffer, 0, read, buffer, 0);
        }

        void HashString(string str)
        {
            var buffer = Encoding.UTF8.GetBytes(str);
            md5.TransformBlock(buffer, 0, buffer.Length, buffer, 0);
        }

        HashFile(gameAssembly);
        if (Directory.Exists(unityLibs))
            foreach (var file in Directory.EnumerateFiles(unityLibs, "*.dll", SearchOption.TopDirectoryOnly))
            {
                HashString(Path.GetFileName(file));
                HashFile(file);
            }
        string renameMap = Path.Combine(bepinex, "DeobfuscationMap.csv.gz");
        if (File.Exists(renameMap)) HashFile(renameMap);
        HashString(typeof(InteropAssemblyGenerator).Assembly.GetName().Version.ToString());
        HashString(typeof(Cpp2IlApi).Assembly.GetName().Version.ToString());
        md5.TransformFinalBlock(Array.Empty<byte>(), 0, 0);
        return BepInEx.Utility.ByteArrayToString(md5.Hash);
    }
}
