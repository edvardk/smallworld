// The map palette dumper (scripts/map_palette.py; docs/reference.md "World map"). Build-time only: compiled and run
// in a throwaway directory by map_palette.py with the pinned JDK, on a classpath of only the pinned server jar and
// its bundled libraries, without network. It bootstraps the block registry (no server, no world) and prints, for
// every block state, what vanilla's map code (MapItem.update) asks of it:
//
//   states file: state.toString() TAB getMapColor id TAB has fluid TAB colour the map records when it stops here
//                (MapItem.getCorrectStateForFluidBlock: a state with a fluid and no sturdy top face counts as
//                its fluid's legacy block, e.g. a waterlogged slab is WATER, a waterlogged stair keeps its colour)
//                TAB 1 if it is its block's default state
//   colours file: MapColor id TAB base RGB (hex), for every id with a colour
//
// Both are in registry order. map_palette.py turns them into server-config/keys/<version>/map-palette.tsv and
// map-colors.tsv.
import java.io.PrintWriter;
import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.core.Direction;
import net.minecraft.server.Bootstrap;
import net.minecraft.world.level.EmptyBlockGetter;
import net.minecraft.world.level.block.Block;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.material.FluidState;
import net.minecraft.world.level.material.MapColor;

public final class Dump {
    private Dump() {}

    public static void main(String[] args) throws Exception {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        EmptyBlockGetter level = EmptyBlockGetter.INSTANCE;
        try (PrintWriter w = new PrintWriter(args[0], "UTF-8")) {
            for (BlockState s : Block.BLOCK_STATE_REGISTRY) {
                FluidState fluid = s.getFluidState();
                boolean sturdy = s.isFaceSturdy(level, BlockPos.ZERO, Direction.UP);
                BlockState surface = !fluid.isEmpty() && !sturdy ? fluid.createLegacyBlock() : s;
                w.print(s.toString());
                w.print('\t');
                w.print(s.getMapColor(level, BlockPos.ZERO).id);
                w.print('\t');
                w.print(fluid.isEmpty() ? 0 : 1);
                w.print('\t');
                w.print(surface.getMapColor(level, BlockPos.ZERO).id);
                w.print('\t');
                w.print(s == s.getBlock().defaultBlockState() ? 1 : 0);
                w.print('\n');
            }
        }
        try (PrintWriter w = new PrintWriter(args[1], "UTF-8")) {
            for (int i = 0; i < 64; i++) {
                MapColor c = MapColor.byId(i);
                if (c != null && c.id == i) {
                    w.print(i + "\t" + String.format("%06x", c.col) + "\n");
                }
            }
        }
    }
}
