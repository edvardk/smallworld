package io.github.edvardk.smallworld.client;

import java.util.function.Function;
import net.minecraft.network.FriendlyByteBuf;
import net.minecraft.network.codec.StreamCodec;
import net.minecraft.network.protocol.common.custom.CustomPacketPayload;
import net.minecraft.resources.Identifier;

/** The channels as Fabric payloads: each one's bytes are exactly Wire's layout (client/mod/WIRE.md). */
final class Payloads {
    private Payloads() {}

    static final String NAMESPACE = "smallworld";

    record Hello(Wire.Hello value) implements CustomPacketPayload {
        static final Type<Hello> TYPE = new Type<>(Identifier.fromNamespaceAndPath(NAMESPACE, "hello"));
        static final StreamCodec<FriendlyByteBuf, Hello> CODEC =
                raw(p -> Wire.encode(p.value()), b -> new Hello(Wire.decodeHello(b)));

        @Override
        public Type<Hello> type() {
            return TYPE;
        }
    }

    record Fps(Wire.Fps value) implements CustomPacketPayload {
        static final Type<Fps> TYPE = new Type<>(Identifier.fromNamespaceAndPath(NAMESPACE, "fps"));
        static final StreamCodec<FriendlyByteBuf, Fps> CODEC =
                raw(p -> Wire.encode(p.value()), b -> new Fps(Wire.decodeFps(b)));

        @Override
        public Type<Fps> type() {
            return TYPE;
        }
    }

    record Config(Wire.Config value) implements CustomPacketPayload {
        static final Type<Config> TYPE = new Type<>(Identifier.fromNamespaceAndPath(NAMESPACE, "config"));
        static final StreamCodec<FriendlyByteBuf, Config> CODEC =
                raw(p -> Wire.encode(p.value()), b -> new Config(Wire.decodeConfig(b)));

        @Override
        public Type<Config> type() {
            return TYPE;
        }
    }

    /** v1.5: the client compass's state (server to client). */
    record Compass(Wire.Compass value) implements CustomPacketPayload {
        static final Type<Compass> TYPE = new Type<>(Identifier.fromNamespaceAndPath(NAMESPACE, "compass"));
        static final StreamCodec<FriendlyByteBuf, Compass> CODEC =
                raw(p -> Wire.encode(p.value()), b -> new Compass(Wire.decodeCompass(b)));

        @Override
        public Type<Compass> type() {
            return TYPE;
        }
    }

    /** v1.5: the client compass's positions (server to client). */
    record Friends(Wire.Friends value) implements CustomPacketPayload {
        static final Type<Friends> TYPE = new Type<>(Identifier.fromNamespaceAndPath(NAMESPACE, "friends"));
        static final StreamCodec<FriendlyByteBuf, Friends> CODEC =
                raw(p -> Wire.encode(p.value()), b -> new Friends(Wire.decodeFriends(b)));

        @Override
        public Type<Friends> type() {
            return TYPE;
        }
    }

    /** A codec that writes the payload's bytes as they are and reads every remaining byte of the packet. */
    private static <T> StreamCodec<FriendlyByteBuf, T> raw(Function<T, byte[]> encode, Function<byte[], T> decode) {
        return StreamCodec.of(
                (buf, value) -> buf.writeBytes(encode.apply(value)),
                buf -> {
                    byte[] data = new byte[buf.readableBytes()];
                    buf.readBytes(data);
                    return decode.apply(data);
                });
    }
}
