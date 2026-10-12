/*
    ChordSmith VST host — offline VST3 rendering as a separate process.

    Copyright (C) 2026 the ChordSmith contributors.

    This program is free software: you can redistribute it and/or modify it under the terms
    of the GNU General Public License as published by the Free Software Foundation, either
    version 3 of the License, or (at your option) any later version.

    This program is distributed in the hope that it will be useful, but WITHOUT ANY
    WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
    PARTICULAR PURPOSE. See the GNU General Public License for more details.

    It is never linked into the MIT-licensed ChordSmith server; the server only spawns this
    binary and reads the wav it writes. Modes:

      vst_host --list --plugin X.vst3
      vst_host --params --plugin X.vst3 [--param-values INDEX] [--param-limit N]
      vst_host --render --plugin X.vst3 [--preset NAME|INDEX] [--set INDEX=VALUE|NAME] \
               [--tail SECONDS] --midi in.mid --wav out.wav
      vst_host --process [--plugin X.vst3 [--preset NAME|INDEX] [--set INDEX=VALUE|NAME]]... \
               --in in.wav --out out.wav

    A --set value may be a number (a plain index for discrete parameters, 0-1 for continuous
    ones) or a value name the plugin resolves itself ("Saw"). --param-values scans a continuous
    parameter and lists the distinct texts it produces — VST3 plugins often expose their choice
    parameters as continuous, so the scan is what yields a usable list for them.

    Every mode prints one JSON line on stdout ({"ok": true, ...} or {"ok": false, "error": ...}).
*/

#include <juce_audio_formats/juce_audio_formats.h>
#include <juce_audio_processors/juce_audio_processors.h>

#include <algorithm>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <limits>
#include <memory>
#include <vector>

namespace {

constexpr double kSampleRate = 44100.0;
constexpr int kBlockSize = 512;

struct Options {
    juce::String mode;
    juce::StringArray plugins;
    juce::StringArray presets;  // one per plugin (empty = the plugin's default state)
    juce::StringArray sets;     // one per plugin: "index=plainValue;index=plainValue;..."
    juce::String midi;
    juce::String wav;
    juce::String input;
    juce::String output;
    double tail = 2.0;
    int parameterValues = -1;
    int parameterLimit = 200;
    bool help = false;
};

juce::String jsonString(const juce::String& value) {
    juce::String out("\"");
    for (auto character : value) {
        switch (character) {
            case '"': out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n"; break;
            case '\r': out += "\\r"; break;
            case '\t': out += "\\t"; break;
            default:
                if (character < 32)
                    out += "?";
                else
                    out += juce::String::charToString(character);
        }
    }
    return out + "\"";
}

int fail(const juce::String& message) {
    std::cout << "{\"ok\":false,\"error\":" << jsonString(message) << "}" << std::endl;
    std::cerr << message << std::endl;
    return 2;
}

void printUsage() {
    std::cout << "usage:\n"
              << "  vst_host --list --plugin X.vst3\n"
              << "  vst_host --params --plugin X.vst3 [--param-values INDEX] [--param-limit N]\n"
              << "  vst_host --render --plugin X.vst3 [--preset NAME|INDEX] [--set INDEX=VALUE|NAME] "
                 "[--tail SECONDS] --midi in.mid --wav out.wav\n"
              << "  vst_host --process [--plugin X.vst3 [--preset NAME|INDEX] [--set INDEX=VALUE|NAME]]... "
                 "--in in.wav --out out.wav\n"
              << "  (--set values may be numbers or value names; --param-values scans continuous "
                 "parameters for their distinct texts)\n";
}

bool parseOptions(int argc, char** argv, Options& options, juce::String& error) {
    juce::StringArray args;
    for (int i = 1; i < argc; ++i) args.add(argv[i]);
    for (int i = 0; i < args.size(); ++i) {
        const auto arg = args[i];
        auto value = [&](const juce::String& name) -> juce::String {
            if (i + 1 >= args.size()) {
                error = name + " needs a value";
                return {};
            }
            return args[++i];
        };
        if (arg == "--help" || arg == "-h") {
            options.help = true;
        } else if (arg == "--list" || arg == "--render" || arg == "--process" || arg == "--params") {
            options.mode = arg;
        } else if (arg == "--plugin") {
            options.plugins.add(value(arg));
            options.presets.add("");
            options.sets.add("");
        } else if (arg == "--preset") {
            if (options.presets.isEmpty()) {
                error = "--preset must follow a --plugin";
                return false;
            }
            options.presets.set(options.presets.size() - 1, value(arg));
        } else if (arg == "--set") {
            if (options.sets.isEmpty()) {
                error = "--set must follow a --plugin";
                return false;
            }
            const int last = options.sets.size() - 1;
            options.sets.set(last, options.sets[last] + value(arg) + ";");
        } else if (arg == "--param-values") {
            options.parameterValues = value(arg).getIntValue();
        } else if (arg == "--param-limit") {
            options.parameterLimit = value(arg).getIntValue();
        } else if (arg == "--midi") {
            options.midi = value(arg);
        } else if (arg == "--wav") {
            options.wav = value(arg);
        } else if (arg == "--in") {
            options.input = value(arg);
        } else if (arg == "--out") {
            options.output = value(arg);
        } else if (arg == "--tail") {
            options.tail = value(arg).getDoubleValue();
        } else {
            error = "Unknown argument '" + arg + "'";
            return false;
        }
        if (error.isNotEmpty()) return false;
    }
    return true;
}

juce::AudioPluginFormatManager& formatManager() {
    static juce::AudioPluginFormatManager* manager = [] {
        auto* created = new juce::AudioPluginFormatManager();
        created->addDefaultFormats();
        return created;
    }();
    return *manager;
}

juce::AudioPluginFormat* vst3Format() {
    for (auto* format : formatManager().getFormats())
        if (format->getName() == "VST3") return format;
    return nullptr;
}

bool loadTypes(
    const juce::String& path, juce::OwnedArray<juce::PluginDescription>& types, juce::String& error) {
    auto* format = vst3Format();
    if (format == nullptr) {
        error = "VST3 support is not available in this build";
        return false;
    }
    if (!juce::File(path).exists()) {
        error = "Plugin '" + path + "' does not exist";
        return false;
    }
    format->findAllTypesForFile(types, path);
    if (types.isEmpty()) {
        error = "No VST3 types found in '" + path + "'";
        return false;
    }
    return true;
}

std::unique_ptr<juce::AudioPluginInstance> instantiate(
    const juce::PluginDescription& description, juce::String& error) {
    juce::String failure;
    auto instance = formatManager().createPluginInstance(description, kSampleRate, kBlockSize, failure);
    if (instance == nullptr)
        error = failure.isNotEmpty() ? failure : "The plugin could not be instantiated";
    return instance;
}

bool applyPreset(juce::AudioPluginInstance& instance, const juce::String& preset, juce::String& error) {
    if (preset.isEmpty()) return true;
    const int count = instance.getNumPrograms();
    if (count <= 0) {
        error = "This plugin exposes no programs; omit --preset for its default state";
        return false;
    }
    int index = -1;
    if (preset.containsOnly("0123456789")) {
        index = preset.getIntValue();
    } else {
        for (int i = 0; i < count && index < 0; ++i)
            if (instance.getProgramName(i).equalsIgnoreCase(preset)) index = i;
        for (int i = 0; i < count && index < 0; ++i)
            if (instance.getProgramName(i).containsIgnoreCase(preset)) index = i;
    }
    if (index < 0 || index >= count) {
        juce::StringArray names;
        for (int i = 0; i < juce::jmin(count, 20); ++i) names.add(instance.getProgramName(i));
        error = "No program matches '" + preset + "'. Programs: " + names.joinIntoString(", ");
        return false;
    }
    instance.setCurrentProgram(index);
    return true;
}

void configureBuses(juce::AudioPluginInstance& instance, bool instrument) {
    // The legacy convenience call is what works across the plugins we run (IK suite, our test
    // plugins): instruments take no audio input, effects a stereo pair. Do not enable all buses
    // first — that leaves several IK plugins with a mis-routed main output and no audio.
    instance.setPlayConfigDetails(instrument ? 0 : 2, 2, kSampleRate, kBlockSize);
}

// JUCE reports a continuous parameter's step count as INT_MAX; the JSON and --set take the
// friendlier convention: 0 = continuous, 1 = toggle, N = discrete.
constexpr int kContinuousSteps = std::numeric_limits<int>::max();

int stepCount(const juce::AudioProcessorParameter& parameter) {
    const int steps = parameter.getNumSteps();
    return steps >= kContinuousSteps ? 0 : steps;
}

int plainValue(juce::AudioProcessorParameter& parameter) {
    const int steps = stepCount(parameter);
    const float value = parameter.getValue();
    if (steps > 1) return static_cast<int>(std::lround(value * (steps - 1)));
    if (steps == 1) return value > 0.5f ? 1 : 0;
    return -1;
}

bool parseNumber(const juce::String& text, double& value) {
    const auto trimmed = text.trim();
    if (trimmed.isEmpty()) return false;
    const char* start = trimmed.toRawUTF8();
    char* end = nullptr;
    value = std::strtod(start, &end);
    if (end == start || end == nullptr || *end != '\0' || !std::isfinite(value)) return false;
    return true;
}

bool sameText(const juce::String& a, const juce::String& b) {
    return a.trim().equalsIgnoreCase(b.trim());
}

// The normalized value for a value name, or -1 when none matches. The plugin's own text->value
// mapping is tried first, then a scan of its discrete states (or the value range, because VST3
// plugins like Surge XT expose their choice parameters as continuous values, steps = 0).
float textValue(juce::AudioProcessorParameter& parameter, const juce::String& text) {
    const float direct = parameter.getValueForText(text);
    if (std::isfinite(direct) && direct >= 0.0f && direct <= 1.0f
        && sameText(parameter.getText(direct, 120), text))
        return direct;
    const int steps = stepCount(parameter);
    if (steps > 0) {
        const int count = steps > 1 ? steps : 2;
        for (int i = 0; i < count; ++i) {
            const float value =
                steps > 1 ? static_cast<float>(i) / static_cast<float>(steps - 1) : (i == 0 ? 0.0f : 1.0f);
            if (sameText(parameter.getText(value, 120), text)) return value;
        }
    } else {
        constexpr int kScanSamples = 1024;
        for (int i = 0; i <= kScanSamples; ++i) {
            const float value = static_cast<float>(i) / static_cast<float>(kScanSamples);
            if (sameText(parameter.getText(value, 120), text)) return value;
        }
    }
    return -1.0f;
}

juce::String availableTexts(juce::AudioProcessorParameter& parameter) {
    const int steps = stepCount(parameter);
    const int count = steps > 1 ? steps : (steps == 1 ? 2 : 33);
    juce::StringArray texts;
    for (int i = 0; i < count && texts.size() < 16; ++i) {
        const float value = steps > 1     ? static_cast<float>(i) / static_cast<float>(steps - 1)
                            : steps == 1  ? (i == 0 ? 0.0f : 1.0f)
                                          : static_cast<float>(i) / static_cast<float>(count - 1);
        const auto text = parameter.getText(value, 120).trim();
        if (text.isNotEmpty() && !texts.contains(text)) texts.add(text);
    }
    return texts.isEmpty() ? juce::String() : " Available: " + texts.joinIntoString(", ");
}

// Returns an error for a value name no state of the parameter produces; warnings for indices
// and numbers stay on stderr so a typo in a *name* never renders a silent no-op.
juce::String applySets(juce::AudioPluginInstance& instance, const juce::String& sets) {
    if (sets.isEmpty()) return {};
    auto& parameters = instance.getParameters();
    for (const auto& entry : juce::StringArray::fromTokens(sets, ";", "")) {
        if (entry.isEmpty()) continue;
        const auto key = entry.upToFirstOccurrenceOf("=", false, false).trim();
        const auto raw = entry.fromFirstOccurrenceOf("=", false, false);
        const int index = key.getIntValue();
        if (key.isEmpty() || !key.containsOnly("0123456789")) {
            std::cerr << "warning: '" << key << "' is not a parameter index" << std::endl;
            continue;
        }
        if (index < 0 || index >= parameters.size()) {
            std::cerr << "warning: parameter " << index << " is out of range (0-"
                      << parameters.size() - 1 << ")" << std::endl;
            continue;
        }
        auto* parameter = parameters[index];
        double number = 0.0;
        if (!parseNumber(raw, number)) {
            const float value = textValue(*parameter, raw);
            if (value < 0.0f)
                return "parameter " + key + " ('" + parameter->getName(120) + "'): no value matches '"
                       + raw.trim() + "'." + availableTexts(*parameter);
            parameter->setValue(value);
            continue;
        }
        const int steps = stepCount(*parameter);
        float normalized = 0.0f;
        if (steps > 1)
            normalized = static_cast<float>(juce::jlimit(0.0, 1.0, number / (steps - 1)));
        else if (steps == 1)
            normalized = number > 0.5 ? 1.0f : 0.0f;
        else
            normalized = static_cast<float>(juce::jlimit(0.0, 1.0, number));
        parameter->setValue(normalized);
    }
    return {};
}

struct TimedMessage {
    double seconds;
    juce::MidiMessage message;
};

bool loadMidi(const juce::String& path, std::vector<TimedMessage>& events, double& end, juce::String& error) {
    juce::File file(path);
    if (!file.existsAsFile()) {
        error = "MIDI file '" + path + "' does not exist";
        return false;
    }
    juce::FileInputStream stream(file);
    if (!stream.openedOk()) {
        error = "Could not open '" + path + "'";
        return false;
    }
    juce::MidiFile midi;
    if (!midi.readFrom(stream)) {
        error = "'" + path + "' is not a readable MIDI file";
        return false;
    }
    midi.convertTimestampTicksToSeconds();
    for (int t = 0; t < midi.getNumTracks(); ++t) {
        const auto* track = midi.getTrack(t);
        if (track == nullptr) continue;
        for (const auto* event : *track)
            if (!event->message.isMetaEvent())
                events.push_back({event->message.getTimeStamp(), event->message});
    }
    std::sort(events.begin(), events.end(),
        [](const TimedMessage& a, const TimedMessage& b) { return a.seconds < b.seconds; });
    end = events.empty() ? 0.0 : events.back().seconds;
    return true;
}

bool readWav(const juce::File& file, juce::AudioBuffer<float>& buffer, juce::String& error) {
    juce::AudioFormatManager manager;
    manager.registerBasicFormats();
    std::unique_ptr<juce::AudioFormatReader> reader(manager.createReaderFor(file));
    if (reader == nullptr) {
        error = "Could not read '" + file.getFullPathName() + "' as audio";
        return false;
    }
    if (std::abs(reader->sampleRate - kSampleRate) > 1.0) {
        error = "Input audio must be 44.1 kHz";
        return false;
    }
    const int channels = juce::jmax(1, static_cast<int>(reader->numChannels));
    const int samples = static_cast<int>(reader->lengthInSamples);
    juce::AudioBuffer<float> source(channels, juce::jmax(1, samples));
    source.clear();
    if (!reader->read(&source, 0, samples, 0, true, true)) {
        error = "Reading '" + file.getFullPathName() + "' failed";
        return false;
    }
    buffer.setSize(2, juce::jmax(1, samples));
    buffer.clear();
    buffer.copyFrom(0, 0, source, 0, 0, samples);
    buffer.copyFrom(1, 0, source, channels > 1 ? 1 : 0, 0, samples);
    return true;
}

bool writeWav(const juce::File& file, const juce::AudioBuffer<float>& buffer, juce::String& error) {
    file.deleteFile();
    auto stream = std::make_unique<juce::FileOutputStream>(file);
    if (!stream->openedOk()) {
        error = "Could not write '" + file.getFullPathName() + "'";
        return false;
    }
    juce::WavAudioFormat format;
    auto* writer = format.createWriterFor(stream.get(), kSampleRate, 2, 16, {}, 0);
    if (writer == nullptr) {
        error = "Could not create a wav writer";
        return false;
    }
    stream.release();
    std::unique_ptr<juce::AudioFormatWriter> holder(writer);
    if (!holder->writeFromAudioSampleBuffer(buffer, 0, buffer.getNumSamples())) {
        error = "Writing '" + file.getFullPathName() + "' failed";
        return false;
    }
    return true;
}

int runList(const Options& options) {
    if (options.plugins.isEmpty()) return fail("--list needs --plugin");
    juce::String error;
    juce::OwnedArray<juce::PluginDescription> types;
    if (!loadTypes(options.plugins[0], types, error)) return fail(error);
    juce::String json = "{\"ok\":true,\"mode\":\"list\",\"plugin\":" + jsonString(options.plugins[0]) + ",\"types\":[";
    for (int t = 0; t < types.size(); ++t) {
        if (t > 0) json += ",";
        const auto& description = *types[t];
        juce::String programs;
        int programCount = 0;
        int parameters = 0;
        int latency = 0;
        juce::String typeError;
        auto instance = instantiate(description, typeError);
        if (instance != nullptr) {
            // Prepare so the wrapper reports real latency (VST3 refreshes it on setupProcessing).
            configureBuses(*instance, description.isInstrument);
            instance->prepareToPlay(kSampleRate, kBlockSize);
            programCount = instance->getNumPrograms();
            parameters = instance->getParameters().size();
            latency = instance->getLatencySamples();
            for (int i = 0; i < juce::jmin(programCount, 200); ++i) {
                if (i > 0) programs += ",";
                programs += jsonString(instance->getProgramName(i));
            }
        }
        json += "{\"name\":" + jsonString(description.name) +
                ",\"vendor\":" + jsonString(description.manufacturerName) +
                ",\"category\":" + jsonString(description.category) +
                ",\"is_instrument\":" + (description.isInstrument ? "true" : "false") +
                ",\"program_count\":" + juce::String(programCount) +
                ",\"programs\":[" + programs + "]" +
                ",\"parameters\":" + juce::String(parameters) +
                ",\"latency_samples\":" + juce::String(latency) +
                ",\"error\":" + (typeError.isEmpty() ? juce::String("null") : jsonString(typeError)) + "}";
    }
    json += "]}";
    std::cout << json << std::endl;
    return 0;
}

int runParams(const Options& options) {
    if (options.plugins.isEmpty()) return fail("--params needs --plugin");
    juce::String error;
    juce::OwnedArray<juce::PluginDescription> types;
    if (!loadTypes(options.plugins[0], types, error)) return fail(error);
    auto instance = instantiate(*types[0], error);
    if (instance == nullptr) return fail(error);
    configureBuses(*instance, types[0]->isInstrument);
    instance->prepareToPlay(kSampleRate, kBlockSize);

    if (options.parameterValues >= 0) {
        auto& parameters = instance->getParameters();
        if (options.parameterValues >= parameters.size())
            return fail("parameter index " + juce::String(options.parameterValues) + " is out of range");
        auto* parameter = parameters[options.parameterValues];
        const int steps = stepCount(*parameter);
        const int limit = juce::jmax(1, options.parameterLimit);
        juce::String values;
        int count = 0;
        bool scanned = false;
        bool truncated = false;
        if (steps > 1) {
            for (int value = 0; value < steps && count < limit; ++value, ++count) {
                if (count > 0) values += ",";
                values += "{\"plain\":" + juce::String(value) + ",\"text\":" +
                          jsonString(parameter->getText((float) value / (float) (steps - 1), 120)) + "}";
            }
            truncated = count < steps;
        } else if (steps == 1) {
            for (int value = 0; value < 2 && count < limit; ++value, ++count) {
                if (count > 0) values += ",";
                values += "{\"plain\":" + juce::String(value) + ",\"text\":" +
                          jsonString(parameter->getText(value == 0 ? 0.0f : 1.0f, 120)) + "}";
            }
            truncated = count < 2;
        } else {
            // VST3 plugins often expose choice parameters as continuous values, so scan the range
            // and compress equal neighbouring texts into runs — a short run list is a selector.
            scanned = true;
            struct Run {
                int first = 0;
                int last = 0;
                juce::String text;
            };
            std::vector<Run> runs;
            constexpr int kScanSamples = 1024;
            for (int i = 0; i <= kScanSamples; ++i) {
                const auto text = parameter->getText((float) i / (float) kScanSamples, 120).trim();
                if (!runs.empty() && runs.back().text.equalsIgnoreCase(text))
                    runs.back().last = i;
                else
                    runs.push_back({i, i, text});
            }
            truncated = static_cast<int>(runs.size()) > limit;
            if (runs.size() > 1 && !truncated) {
                for (size_t r = 0; r < runs.size(); ++r) {
                    if (r > 0) values += ",";
                    const float midpoint =
                        (static_cast<float>(runs[r].first) + static_cast<float>(runs[r].last)) / 2.0f
                        / static_cast<float>(kScanSamples);
                    values += "{\"plain\":" + juce::String(static_cast<int>(r)) + ",\"text\":" +
                              jsonString(runs[r].text) + ",\"value\":" + juce::String(midpoint, 6) + "}";
                }
            }
        }
        std::cout << "{\"ok\":true,\"mode\":\"values\",\"plugin\":" << jsonString(types[0]->name)
                  << ",\"index\":" << options.parameterValues
                  << ",\"name\":" << jsonString(parameter->getName(120))
                  << ",\"steps\":" << steps
                  << ",\"scanned\":" << (scanned ? "true" : "false")
                  << ",\"truncated\":" << (truncated ? "true" : "false")
                  << ",\"values\":[" << values << "]}" << std::endl;
        return 0;
    }

    auto& parameters = instance->getParameters();
    juce::String json = "{\"ok\":true,\"mode\":\"params\",\"plugin\":" + jsonString(types[0]->name) +
                        ",\"parameters\":[";
    for (int i = 0; i < parameters.size(); ++i) {
        if (i > 0) json += ",";
        auto* parameter = parameters[i];
        const int steps = stepCount(*parameter);
        const float value = parameter->getValue();
        json += "{\"index\":" + juce::String(i) +
                ",\"name\":" + jsonString(parameter->getName(120)) +
                ",\"steps\":" + juce::String(steps) +
                ",\"value\":" + juce::String(value, 4) +
                ",\"plain\":" + juce::String(plainValue(*parameter)) +
                ",\"text\":" + jsonString(parameter->getText(value, 120)) + "}";
    }
    json += "]}";
    std::cout << json << std::endl;
    return 0;
}

int runRender(const Options& options) {
    if (options.plugins.isEmpty()) return fail("--render needs --plugin");
    if (options.midi.isEmpty() || options.wav.isEmpty())
        return fail("--render needs --midi and --wav");
    juce::String error;
    juce::OwnedArray<juce::PluginDescription> types;
    if (!loadTypes(options.plugins[0], types, error)) return fail(error);
    if (!types[0]->isInstrument)
        return fail("'" + types[0]->name + "' is an effect, not an instrument; use --process for effects");
    auto instance = instantiate(*types[0], error);
    if (instance == nullptr) return fail(error);
    configureBuses(*instance, true);
    instance->prepareToPlay(kSampleRate, kBlockSize);
    if (!applyPreset(*instance, options.presets[0], error)) return fail(error);
    if (const auto setsError = applySets(*instance, options.sets[0]); setsError.isNotEmpty())
        return fail(setsError);

    std::vector<TimedMessage> events;
    double end = 0.0;
    if (!loadMidi(options.midi, events, end, error)) return fail(error);

    const juce::int64 totalFrames =
        static_cast<juce::int64>(std::ceil((end + juce::jmax(0.0, options.tail)) * kSampleRate));
    juce::AudioBuffer<float> buffer(2, kBlockSize);
    juce::MidiBuffer midiBuffer;
    juce::File outFile(options.wav);
    outFile.deleteFile();
    auto stream = std::make_unique<juce::FileOutputStream>(outFile);
    if (!stream->openedOk()) return fail("Could not write '" + options.wav + "'");
    juce::WavAudioFormat wavFormat;
    auto* writer = wavFormat.createWriterFor(stream.get(), kSampleRate, 2, 16, {}, 0);
    if (writer == nullptr) return fail("Could not create the wav writer");
    stream.release();
    std::unique_ptr<juce::AudioFormatWriter> holder(writer);

    size_t next = 0;
    for (juce::int64 start = 0; start < totalFrames; start += kBlockSize) {
        midiBuffer.clear();
        while (next < events.size() &&
               events[next].seconds * kSampleRate < static_cast<double>(start + kBlockSize)) {
            const int offset = juce::jlimit(0, kBlockSize - 1,
                static_cast<int>(events[next].seconds * kSampleRate) - static_cast<int>(start));
            midiBuffer.addEvent(events[next].message, offset);
            ++next;
        }
        buffer.clear();
        instance->processBlock(buffer, midiBuffer);
        holder->writeFromAudioSampleBuffer(buffer, 0, kBlockSize);
    }
    const double seconds = static_cast<double>(totalFrames) / kSampleRate;
    std::cout << "{\"ok\":true,\"mode\":\"render\",\"plugin\":" << jsonString(types[0]->name)
              << ",\"frames\":" << totalFrames
              << ",\"seconds\":" << juce::String(seconds, 3)
              << ",\"latency_samples\":" << instance->getLatencySamples() << "}" << std::endl;
    return 0;
}

int runProcess(const Options& options) {
    if (options.plugins.isEmpty()) return fail("--process needs at least one --plugin");
    if (options.input.isEmpty() || options.output.isEmpty())
        return fail("--process needs --in and --out");
    juce::String error;
    juce::AudioBuffer<float> audio;
    if (!readWav(juce::File(options.input), audio, error)) return fail(error);
    const int samples = audio.getNumSamples();

    std::vector<std::unique_ptr<juce::AudioPluginInstance>> chain;
    int latency = 0;
    for (int i = 0; i < options.plugins.size(); ++i) {
        juce::OwnedArray<juce::PluginDescription> types;
        if (!loadTypes(options.plugins[i], types, error)) return fail(error);
        if (types[0]->isInstrument)
            return fail("'" + types[0]->name + "' is an instrument; --process takes effects");
        auto instance = instantiate(*types[0], error);
        if (instance == nullptr) return fail(error);
        configureBuses(*instance, false);
        instance->prepareToPlay(kSampleRate, kBlockSize);
        if (!applyPreset(*instance, options.presets[i], error)) return fail(error);
        if (const auto setsError = applySets(*instance, options.sets[i]); setsError.isNotEmpty())
            return fail(setsError);
        latency += instance->getLatencySamples();
        chain.push_back(std::move(instance));
    }

    juce::MidiBuffer midiBuffer;
    for (int start = 0; start < samples; start += kBlockSize) {
        const int blockFrames = juce::jmin(kBlockSize, samples - start);
        juce::AudioBuffer<float> chunk(audio.getArrayOfWritePointers(), 2, start, blockFrames);
        for (auto& effect : chain) effect->processBlock(chunk, midiBuffer);
    }
    // The chain delayed the audio by the sum of the plugins' reported latencies; shift it back
    // so the processed stem stays aligned with every other stem (zeros pad the end).
    if (latency > 0 && latency < samples) {
        for (int channel = 0; channel < 2; ++channel) {
            audio.copyFrom(channel, 0, audio, channel, latency, samples - latency);
            audio.clear(channel, samples - latency, latency);
        }
    }
    if (!writeWav(juce::File(options.output), audio, error)) return fail(error);
    std::cout << "{\"ok\":true,\"mode\":\"process\",\"frames\":" << samples
              << ",\"seconds\":" << juce::String(static_cast<double>(samples) / kSampleRate, 3)
              << ",\"latency_samples\":" << latency << "}" << std::endl;
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    juce::ScopedJuceInitialiser_GUI initialise;
    Options options;
    juce::String error;
    if (!parseOptions(argc, argv, options, error)) return fail(error);
    if (options.help || options.mode.isEmpty()) {
        printUsage();
        return options.mode.isEmpty() && !options.help ? 2 : 0;
    }
    if (options.mode == "--list") return runList(options);
    if (options.mode == "--params") return runParams(options);
    if (options.mode == "--render") return runRender(options);
    if (options.mode == "--process") return runProcess(options);
    return fail("Unknown mode '" + options.mode + "'");
}
