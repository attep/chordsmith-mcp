/*
    ToneSynth — the tiny test instrument for the ChordSmith VST host tests.
    GPL-3.0-or-later (see ../LICENSE). Not for production use.

    A polyphonic sine/saw with a short attack and release, two programs ("Sine", "Saw"), a
    "Wave" choice parameter (setting it by name exercises the host's text -> value path) and
    no editor, so the host can be exercised end to end without third-party plugins.
*/

#include <juce_audio_processors/juce_audio_processors.h>

#include <algorithm>
#include <cmath>
#include <vector>

namespace {

class ToneSynthProcessor : public juce::AudioProcessor {
public:
    ToneSynthProcessor()
        : AudioProcessor(BusesProperties().withOutput("Output", juce::AudioChannelSet::stereo(), true)) {
        gain = new juce::AudioParameterFloat("gain", "Gain", 0.0f, 1.0f, 0.8f);
        addParameter(gain);  // the processor takes ownership
        wave = new juce::AudioParameterChoice("wave", "Wave", juce::StringArray{"Sine", "Saw"}, 0);
        addParameter(wave);  // the processor takes ownership
    }

    const juce::String getName() const override { return "ToneSynth"; }
    bool acceptsMidi() const override { return true; }
    bool producesMidi() const override { return false; }
    bool isMidiEffect() const override { return false; }
    double getTailLengthSeconds() const override { return 1.0; }
    juce::AudioProcessorEditor* createEditor() override { return nullptr; }
    bool hasEditor() const override { return false; }

    void prepareToPlay(double rate, int) override {
        sampleRate = rate;
        voices.clear();
    }
    void releaseResources() override { voices.clear(); }

    bool isBusesLayoutSupported(const BusesLayout& layouts) const override {
        return layouts.getMainOutputChannelSet() == juce::AudioChannelSet::stereo();
    }

    void processBlock(juce::AudioBuffer<float>& buffer, juce::MidiBuffer& midi) override {
        buffer.clear();
        for (const auto metadata : midi) {
            const auto message = metadata.getMessage();
            if (message.isNoteOn()) {
                voices.push_back({message.getNoteNumber(), 0.0, 0.0f,
                    message.getFloatVelocity(), false});
            } else if (message.isNoteOff()) {
                for (auto& voice : voices)
                    if (voice.note == message.getNoteNumber()) voice.releasing = true;
            } else if (message.isAllNotesOff()) {
                for (auto& voice : voices) voice.releasing = true;
            }
        }
        const float attackStep = static_cast<float>(1.0 / (0.005 * sampleRate));
        const float releaseStep = static_cast<float>(1.0 / (0.08 * sampleRate));
        const int frames = buffer.getNumSamples();
        const bool sawtooth = wave->getIndex() == 1;
        for (int frame = 0; frame < frames; ++frame) {
            float sample = 0.0f;
            for (auto& voice : voices) {
                if (voice.releasing)
                    voice.level = juce::jmax(0.0f, voice.level - releaseStep);
                else if (voice.level < 1.0f)
                    voice.level = juce::jmin(1.0f, voice.level + attackStep);
                const double frequency = 440.0 * std::pow(2.0, (voice.note - 69) / 12.0);
                voice.phase += frequency / sampleRate;
                if (voice.phase >= 1.0) voice.phase -= 1.0;
                const auto waveform = sawtooth
                    ? static_cast<float>(2.0 * (voice.phase - std::floor(voice.phase + 0.5)))
                    : static_cast<float>(std::sin(voice.phase * juce::MathConstants<double>::twoPi));
                sample += waveform * voice.level * voice.velocity * 0.25f;
            }
            voices.erase(
                std::remove_if(voices.begin(), voices.end(),
                    [](const Voice& voice) { return voice.releasing && voice.level <= 0.0f; }),
                voices.end());
            for (int channel = 0; channel < buffer.getNumChannels(); ++channel)
                buffer.setSample(channel, frame, sample * gain->get());
        }
    }

    int getNumPrograms() override { return 2; }
    int getCurrentProgram() override { return program; }
    void setCurrentProgram(int index) override {
        program = juce::jlimit(0, 1, index);
        *wave = program;  // keep the Wave parameter in step with the program
    }
    const juce::String getProgramName(int index) override { return index == 1 ? "Saw" : "Sine"; }
    void changeProgramName(int, const juce::String&) override {}

    void getStateInformation(juce::MemoryBlock& destination) override {
        juce::MemoryOutputStream stream(destination, false);
        stream.writeFloat(gain->get());
        stream.writeInt(wave->getIndex());
    }

    void setStateInformation(const void* data, int sizeInBytes) override {
        juce::MemoryInputStream stream(data, static_cast<size_t>(sizeInBytes), false);
        if (stream.getNumBytesRemaining() < static_cast<juce::int64>(sizeof(float) + sizeof(int))) return;
        gain->setValue(stream.readFloat());
        program = juce::jlimit(0, 1, stream.readInt());
        *wave = program;
    }

private:
    struct Voice {
        int note;
        double phase;
        float level;
        float velocity;
        bool releasing;
    };

    std::vector<Voice> voices;
    juce::AudioParameterFloat* gain = nullptr;    // owned by the processor (addParameter)
    juce::AudioParameterChoice* wave = nullptr;   // owned by the processor (addParameter)
    double sampleRate = 44100.0;
    int program = 0;
};

}  // namespace

juce::AudioProcessor* JUCE_CALLTYPE createPluginFilter() {
    return new ToneSynthProcessor();
}
